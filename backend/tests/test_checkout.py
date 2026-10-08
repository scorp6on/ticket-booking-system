from app.services.bank import MockBank
from app.services.checkout import (
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
from app.services.seats import claim_seat


def seat_row(conn, seat_id):
    return conn.execute("SELECT status, held_by FROM seats WHERE id = %s", (seat_id,)).fetchone()


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM payments ORDER BY id").fetchall()]


def alice_pays(conn, seat_id, bank, key):
    return pay(conn, bank, seat_id, "alice", 4999, key)


def test_approved_payment_sells_the_seat(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["approve"])

    assert alice_pays(conn, seat_id, bank, "k1").outcome == SOLD
    assert seat_row(conn, seat_id) == ("sold", "alice")
    assert statuses(conn) == ["captured"]
    assert len(bank.captured) == 1


def test_three_declines_release_the_seat(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["decline", "decline", "decline"])

    first = alice_pays(conn, seat_id, bank, "k1")
    second = alice_pays(conn, seat_id, bank, "k2")
    third = alice_pays(conn, seat_id, bank, "k3")

    assert (first.outcome, first.attempts_left) == (DECLINED, 2)
    assert (second.outcome, second.attempts_left) == (DECLINED, 1)
    assert third.outcome == SEAT_RELEASED
    assert seat_row(conn, seat_id) == ("open", None)
    assert statuses(conn) == ["declined", "declined", "declined"]
    # A 4th try never reaches the bank, and the seat is free for someone else.
    assert alice_pays(conn, seat_id, bank, "k4").outcome == NOT_HOLDER
    assert len(bank.authorized) == 3
    assert claim_seat(conn, seat_id, "bob") is True


def test_approval_on_the_third_attempt_still_sells(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["decline", "decline", "approve"])

    alice_pays(conn, seat_id, bank, "k1")
    alice_pays(conn, seat_id, bank, "k2")
    assert alice_pays(conn, seat_id, bank, "k3").outcome == SOLD
    assert seat_row(conn, seat_id) == ("sold", "alice")


def test_double_click_asks_the_bank_once(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["approve"])

    assert alice_pays(conn, seat_id, bank, "k1").outcome == SOLD
    assert alice_pays(conn, seat_id, bank, "k1").outcome == SOLD  # same answer, seat already sold
    assert len(bank.authorized) == 1


def test_no_reply_leaves_payment_pending_and_blocks_retries(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["timeout"])

    assert alice_pays(conn, seat_id, bank, "k1").outcome == NO_REPLY
    assert statuses(conn) == ["pending"]
    assert seat_row(conn, seat_id) == ("payment_pending", "alice")
    assert alice_pays(conn, seat_id, bank, "k2").outcome == IN_PROGRESS
    assert len(bank.authorized) == 1


def test_approval_after_the_job_voided_it_is_voided_at_the_bank(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    # While we wait on the bank, the background job decides the payment is stuck.
    bank = MockBank(["approve"], on_authorize=lambda: void_stuck_payments(conn, max_age_seconds=0))

    assert alice_pays(conn, seat_id, bank, "k1").outcome == EXPIRED
    assert statuses(conn) == ["voided"]
    assert bank.voided == bank.authorized
    assert bank.captured == []


def test_approval_after_the_seat_window_closed_is_voided(seat_id, conn):
    claim_seat(conn, seat_id, "alice")

    def window_runs_out():
        conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", (seat_id,))
        conn.commit()

    bank = MockBank(["approve"], on_authorize=window_runs_out)

    assert alice_pays(conn, seat_id, bank, "k1").outcome == EXPIRED
    assert statuses(conn) == ["voided"]
    assert bank.voided == bank.authorized
    assert bank.captured == []
    assert claim_seat(conn, seat_id, "bob") is True  # seat is up for grabs again


def test_someone_without_a_hold_cannot_pay(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    bank = MockBank(["approve"])

    assert pay(conn, bank, seat_id, "bob", 4999, "k1").outcome == NOT_HOLDER
    assert bank.authorized == []
    assert statuses(conn) == []
