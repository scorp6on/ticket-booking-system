from dataclasses import dataclass

import psycopg

from app.services import payments, seats
from app.services.bank import MockBank

# What Alice sees after pressing Pay.
SOLD = "sold"
DECLINED = "declined"            # try another card; attempts_left says how many tries remain
SEAT_RELEASED = "seat_released"  # 3rd decline: the seat is back on sale
NO_REPLY = "no_reply"            # bank didn't answer; the background job voids it after the window
EXPIRED = "expired"              # payment window ran out; any held money is released
NOT_HOLDER = "not_holder"        # no hold or payment window on this seat
IN_PROGRESS = "in_progress"      # another payment for this seat is still waiting on the bank

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


def pay(
    conn: psycopg.Connection,
    bank: MockBank,
    seat_id: int,
    user_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> CheckoutResult:
    # A repeat must get its original answer even if the seat has since been sold,
    # so check the key before looking at the seat.
    repeat = _repeat_answer(conn, idempotency_key)
    if repeat:
        return repeat

    # First try moves the seat from 'held' to 'payment_pending'. On a retry it's
    # already there, so start_payment returns False and we check the window instead.
    if not seats.start_payment(conn, seat_id, user_id) and not seats.in_payment_window(conn, seat_id, user_id):
        return CheckoutResult(NOT_HOLDER)

    created = payments.create_payment(conn, seat_id, user_id, amount_cents, idempotency_key)
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
        attempts_left = seats.record_declined_payment(conn, seat_id, user_id)
        if attempts_left is None:
            return CheckoutResult(EXPIRED)
        if attempts_left == 0:
            return CheckoutResult(SEAT_RELEASED)
        return CheckoutResult(DECLINED, attempts_left)

    if not payments.mark_approved(conn, payment_id, bank_id):
        bank.void(payment_id)  # the job already voided it: late approval
        return CheckoutResult(EXPIRED)

    if not seats.confirm_payment(conn, seat_id, user_id):
        bank.void(payment_id)  # approved in time, but the seat's window closed first
        payments.mark_voided(conn, payment_id)
        return CheckoutResult(EXPIRED)

    bank.capture(bank_id)
    payments.mark_captured(conn, payment_id)
    return CheckoutResult(SOLD)


def _repeat_answer(conn: psycopg.Connection, idempotency_key: str) -> CheckoutResult | None:
    row = conn.execute("SELECT status FROM payments WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
    conn.commit()
    return None if row is None else CheckoutResult(REPEAT_ANSWER[row[0]])
