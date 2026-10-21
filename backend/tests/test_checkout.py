import pytest

from app.services.bank import MockBank
from app.services.carts import hold_seats
from app.services.checkout import (
    CART_CHANGED,
    DECLINED,
    EXPIRED,
    IN_PROGRESS,
    NO_REPLY,
    NOT_HOLDER,
    SEAT_RELEASED,
    SOLD,
    pay,
)
from app.services.payments import void_stuck_payments


def seat_row(conn, seat_id):
    return conn.execute("SELECT status, held_by FROM seats WHERE id = %s", (seat_id,)).fetchone()


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM payments ORDER BY id").fetchall()]


@pytest.fixture
def alice(account):
    return account("alice")


@pytest.fixture
def cart(conn, event_id, seat_id, alice):
    """Alice holds seat 14B in a cart of one."""
    return hold_seats(conn, alice, event_id, [seat_id]).cart_id


def alice_pays(conn, alice, cart, seat_id, bank, key):
    return pay(conn, bank, alice, cart, [seat_id], key)


def test_approved_payment_sells_the_seat(conn, alice, cart, seat_id):
    bank = MockBank(["approve"])

    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == SOLD
    assert seat_row(conn, seat_id) == ("sold", str(alice))
    assert statuses(conn) == ["captured"]
    assert len(bank.captured) == 1


def test_one_payment_for_the_whole_cart(conn, event_id, seats, alice):
    cart = hold_seats(conn, alice, event_id, seats[:3]).cart_id
    bank = MockBank(["approve"])

    assert pay(conn, bank, alice, cart, seats[:3], "k1").outcome == SOLD
    assert [seat_row(conn, s)[0] for s in seats[:3]] == ["sold"] * 3
    assert conn.execute("SELECT amount_cents FROM payments").fetchall() == [(3 * 4999,)]
    assert len(bank.authorized) == 1


def test_three_declines_release_the_seat(conn, alice, cart, seat_id, account):
    bank = MockBank(["decline", "decline", "decline"])

    first = alice_pays(conn, alice, cart, seat_id, bank, "k1")
    second = alice_pays(conn, alice, cart, seat_id, bank, "k2")
    third = alice_pays(conn, alice, cart, seat_id, bank, "k3")

    assert (first.outcome, first.attempts_left) == (DECLINED, 2)
    assert (second.outcome, second.attempts_left) == (DECLINED, 1)
    assert third.outcome == SEAT_RELEASED
    assert seat_row(conn, seat_id) == ("open", None)
    assert statuses(conn) == ["declined", "declined", "declined"]
    # A 4th try never reaches the bank, and the seat is free for someone else.
    assert alice_pays(conn, alice, cart, seat_id, bank, "k4").outcome == NOT_HOLDER
    assert len(bank.authorized) == 3


def test_approval_on_the_third_attempt_still_sells(conn, alice, cart, seat_id):
    bank = MockBank(["decline", "decline", "approve"])

    alice_pays(conn, alice, cart, seat_id, bank, "k1")
    alice_pays(conn, alice, cart, seat_id, bank, "k2")
    assert alice_pays(conn, alice, cart, seat_id, bank, "k3").outcome == SOLD
    assert seat_row(conn, seat_id) == ("sold", str(alice))


def test_double_click_asks_the_bank_once(conn, alice, cart, seat_id):
    bank = MockBank(["approve"])

    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == SOLD
    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == SOLD  # same answer, seat already sold
    assert len(bank.authorized) == 1


def test_no_reply_leaves_payment_pending_and_blocks_retries(conn, alice, cart, seat_id):
    bank = MockBank(["timeout"])

    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == NO_REPLY
    assert statuses(conn) == ["pending"]
    assert seat_row(conn, seat_id) == ("payment_pending", str(alice))
    assert alice_pays(conn, alice, cart, seat_id, bank, "k2").outcome == IN_PROGRESS
    assert len(bank.authorized) == 1


def test_approval_after_the_job_voided_it_is_voided_at_the_bank(conn, alice, cart, seat_id):
    # While we wait on the bank, the background job decides the payment is stuck.
    bank = MockBank(["approve"], on_authorize=lambda: void_stuck_payments(conn, max_age_seconds=0))

    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == EXPIRED
    assert statuses(conn) == ["voided"]
    assert bank.voided == bank.authorized
    assert bank.captured == []


def test_approval_after_the_window_closed_is_voided(conn, event_id, alice, cart, seat_id, account):
    def window_runs_out():
        conn.execute("UPDATE carts SET expires_at = now() - interval '1 second' WHERE id = %s", (cart,))
        conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", (seat_id,))
        conn.commit()

    bank = MockBank(["approve"], on_authorize=window_runs_out)

    assert alice_pays(conn, alice, cart, seat_id, bank, "k1").outcome == EXPIRED
    assert statuses(conn) == ["voided"]
    assert bank.voided == bank.authorized
    assert bank.captured == []
    assert hold_seats(conn, account("bob"), event_id, [seat_id]).outcome == "held"  # up for grabs again


def test_someone_without_a_hold_cannot_pay(conn, cart, seat_id, account):
    bank = MockBank(["approve"])

    assert pay(conn, bank, account("bob"), cart, [seat_id], "k1").outcome == NOT_HOLDER
    assert bank.authorized == []
    assert statuses(conn) == []


def test_lost_seat_is_named_and_nothing_is_charged(conn, event_id, seats, alice, account):
    cart = hold_seats(conn, alice, event_id, seats[:2]).cart_id
    conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", (seats[1],))
    conn.commit()
    hold_seats(conn, account("bob"), event_id, [seats[1]])
    bank = MockBank(["approve"])

    result = pay(conn, bank, alice, cart, seats[:2], "k1")

    assert (result.outcome, result.expired_seat_ids) == (EXPIRED, [seats[1]])
    assert bank.authorized == []


def test_retry_with_a_different_set_of_seats_is_rejected(conn, event_id, seats, alice):
    cart = hold_seats(conn, alice, event_id, seats[:2]).cart_id
    bank = MockBank(["decline"])
    pay(conn, bank, alice, cart, seats[:2], "k1")

    assert pay(conn, bank, alice, cart, seats[:1], "k2").outcome == CART_CHANGED
    assert len(bank.authorized) == 1
