from dataclasses import dataclass, field

import psycopg

from app.services import carts, payments
from app.services.bank import MockBank

# What Alice sees after pressing Pay.
SOLD = "sold"
DECLINED = "declined"            # try another card; attempts_left says how many tries remain
SEAT_RELEASED = "seat_released"  # 3rd decline: every seat in the cart is back on sale
NO_REPLY = "no_reply"            # bank didn't answer; the background job voids it after the window
EXPIRED = "expired"              # some seats on screen are no longer held (expired_seat_ids), or the window ran out
NOT_HOLDER = "not_holder"        # nothing in this cart is held by this buyer
CART_CHANGED = "cart_changed"    # the cart holds seats the buyer's screen didn't send: show the real cart
IN_PROGRESS = "in_progress"      # another payment for this cart is still waiting on the bank

# A repeated idempotency key gets the answer its first request produced.
REPEAT_ANSWER = {
    "captured": SOLD,
    "declined": DECLINED,
    "voided": EXPIRED,
    "pending": IN_PROGRESS,
    "approved": IN_PROGRESS,
}


@dataclass
class CheckoutResult:
    outcome: str
    attempts_left: int | None = None
    expired_seat_ids: list[int] = field(default_factory=list)


def pay(
    conn: psycopg.Connection,
    bank: MockBank,
    account_id: int,
    cart_id: int,
    seat_ids: list[int],
    idempotency_key: str,
) -> CheckoutResult:
    """One payment for the seats on the buyer's screen. The amount comes from the database."""
    # A repeat must get its original answer even if the seats have since been sold,
    # so check the key before looking at the seats.
    repeat = _repeat_answer(conn, idempotency_key)
    if repeat:
        return repeat

    user_id = str(account_id)
    seat_ids = sorted(set(seat_ids))
    # First try moves the seats from 'held' to 'payment_pending'. On a retry they're
    # already there, and must be exactly the seats on screen.
    paying = carts.paying_seats(conn, account_id, cart_id)
    if paying:
        if paying != set(seat_ids):
            return CheckoutResult(CART_CHANGED)
    else:
        started = carts.start_payment(conn, account_id, cart_id, seat_ids)
        if started.outcome != "started":
            return CheckoutResult(started.outcome, expired_seat_ids=started.expired)

    amount_cents = carts.price(conn, seat_ids)
    created = payments.create_payment(conn, cart_id, user_id, amount_cents, idempotency_key)
    if created is None:
        return CheckoutResult(IN_PROGRESS)
    payment_id, is_new = created
    if not is_new:
        # Two clicks raced past the check above; the other one is talking to the bank.
        return _repeat_answer(conn, idempotency_key)

    try:
        bank_id = bank.authorize(payment_id, amount_cents)
    except TimeoutError:
        return CheckoutResult(NO_REPLY)  # stays 'pending'; void_stuck_payments cleans it up

    if bank_id is None:
        payments.mark_declined(conn, payment_id)
        attempts_left = carts.record_declined_payment(conn, account_id, cart_id)
        if attempts_left is None:
            return CheckoutResult(EXPIRED)
        if attempts_left == 0:
            return CheckoutResult(SEAT_RELEASED)
        return CheckoutResult(DECLINED, attempts_left)

    if not payments.mark_approved(conn, payment_id, bank_id):
        bank.void(payment_id)  # the job already voided it: late approval
        return CheckoutResult(EXPIRED)

    if not carts.confirm_payment(conn, account_id, cart_id, seat_ids):
        bank.void(payment_id)  # approved in time, but the cart's window closed first
        payments.mark_voided(conn, payment_id)
        return CheckoutResult(EXPIRED)

    bank.capture(bank_id)
    payments.mark_captured(conn, payment_id)
    return CheckoutResult(SOLD)


def _repeat_answer(conn: psycopg.Connection, idempotency_key: str) -> CheckoutResult | None:
    row = conn.execute("SELECT status FROM payments WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
    conn.commit()
    return None if row is None else CheckoutResult(REPEAT_ANSWER[row[0]])
