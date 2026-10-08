import threading

import psycopg

from app.config import settings
from app.services.payments import (
    create_payment,
    mark_approved,
    mark_captured,
    mark_declined,
    mark_voided,
    void_stuck_payments,
)


def payment_status(conn, payment_id):
    return conn.execute("SELECT status FROM payments WHERE id = %s", (payment_id,)).fetchone()[0]


def new_payment(conn, seat_id, user_id="alice", key="key-1"):
    payment_id, created = create_payment(conn, seat_id, user_id, 4999, key)
    assert created
    return payment_id


# --- Idempotency: the same request twice is one payment ---

def test_same_key_twice_returns_the_same_payment(seat_id, conn):
    first = create_payment(conn, seat_id, "alice", 4999, "key-1")
    second = create_payment(conn, seat_id, "alice", 4999, "key-1")
    assert first == (first[0], True)
    assert second == (first[0], False)  # same payment, flagged as a repeat
    assert conn.execute("SELECT count(*) FROM payments").fetchone()[0] == 1


def test_20_simultaneous_double_clicks_create_one_payment(seat_id, conn):
    clicks = 20
    start_line = threading.Barrier(clicks)
    results = []

    def click():
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results.append(create_payment(c, seat_id, "alice", 4999, "key-1"))

    threads = [threading.Thread(target=click) for _ in range(clicks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == clicks
    assert sum(1 for _, created in results if created) == 1
    assert len({payment_id for payment_id, _ in results}) == 1
    assert conn.execute("SELECT count(*) FROM payments").fetchone()[0] == 1


# --- One pending payment at a time ---

def test_new_attempt_blocked_while_one_is_pending(seat_id, conn):
    new_payment(conn, seat_id, key="key-1")
    assert create_payment(conn, seat_id, "alice", 4999, "key-2") is None


def test_new_attempt_allowed_after_a_decline(seat_id, conn):
    first = new_payment(conn, seat_id, key="key-1")
    assert mark_declined(conn, first) is True
    second = new_payment(conn, seat_id, key="key-2")
    assert second != first


def test_someone_elses_stuck_payment_does_not_block_a_new_buyer(seat_id, conn):
    new_payment(conn, seat_id, user_id="alice", key="key-1")  # bank never replies
    assert create_payment(conn, seat_id, "bob", 4999, "key-2") is not None


# --- The arrows ---

def test_approve_then_capture(seat_id, conn):
    p = new_payment(conn, seat_id)
    assert mark_approved(conn, p, "bank-123") is True
    assert mark_captured(conn, p) is True
    assert conn.execute("SELECT status, bank_id FROM payments WHERE id = %s", (p,)).fetchone() == (
        "captured",
        "bank-123",
    )


def test_cannot_capture_before_approval(seat_id, conn):
    p = new_payment(conn, seat_id)
    assert mark_captured(conn, p) is False
    assert payment_status(conn, p) == "pending"


def test_approved_payment_can_be_voided(seat_id, conn):
    # Alice lost the seat after the bank approved: release the held money.
    p = new_payment(conn, seat_id)
    mark_approved(conn, p, "bank-123")
    assert mark_voided(conn, p) is True
    assert payment_status(conn, p) == "voided"


def test_final_states_never_change(seat_id, conn):
    declined = new_payment(conn, seat_id, key="key-1")
    mark_declined(conn, declined)
    voided = new_payment(conn, seat_id, key="key-2")
    mark_voided(conn, voided)
    captured = new_payment(conn, seat_id, key="key-3")
    mark_approved(conn, captured, "bank-123")
    mark_captured(conn, captured)

    for p in (declined, voided, captured):
        before = payment_status(conn, p)
        assert mark_approved(conn, p, "bank-999") is False
        assert mark_declined(conn, p) is False
        assert mark_captured(conn, p) is False
        assert mark_voided(conn, p) is False
        assert payment_status(conn, p) == before


# --- No reply from the bank ---

def test_background_job_voids_only_old_pending_payments(seat_id, conn):
    old = new_payment(conn, seat_id, user_id="alice", key="key-1")
    conn.execute("UPDATE payments SET created_at = now() - interval '6 minutes' WHERE id = %s", (old,))
    conn.commit()
    fresh = new_payment(conn, seat_id, user_id="bob", key="key-2")
    approved = new_payment(conn, seat_id, user_id="carol", key="key-3")
    conn.execute("UPDATE payments SET created_at = now() - interval '6 minutes' WHERE id = %s", (approved,))
    conn.commit()
    mark_approved(conn, approved, "bank-123")

    assert void_stuck_payments(conn) == [old]
    assert payment_status(conn, old) == "voided"
    assert payment_status(conn, fresh) == "pending"
    assert payment_status(conn, approved) == "approved"


def test_approval_after_the_job_voided_it_is_rejected(seat_id, conn):
    p = new_payment(conn, seat_id)
    assert void_stuck_payments(conn, max_age_seconds=0) == [p]
    # The bank's "yes" finally arrives. It loses: the caller must void it at the bank.
    assert mark_approved(conn, p, "bank-123") is False
    assert payment_status(conn, p) == "voided"
