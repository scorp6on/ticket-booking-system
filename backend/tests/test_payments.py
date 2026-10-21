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


def new_payment(conn, cart_id, user_id="alice", key="key-1"):
    payment_id, created = create_payment(conn, cart_id, user_id, 4999, key)
    assert created
    return payment_id


# --- Idempotency: the same request twice is one payment ---

def test_same_key_twice_returns_the_same_payment(conn, new_cart):
    cart_id = new_cart()
    first = create_payment(conn, cart_id, "alice", 4999, "key-1")
    second = create_payment(conn, cart_id, "alice", 4999, "key-1")
    assert first == (first[0], True)
    assert second == (first[0], False)  # same payment, flagged as a repeat
    assert conn.execute("SELECT count(*) FROM payments").fetchone()[0] == 1


def test_20_simultaneous_double_clicks_create_one_payment(conn, new_cart):
    cart_id = new_cart()
    clicks = 20
    start_line = threading.Barrier(clicks)
    results = []

    def click():
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results.append(create_payment(c, cart_id, "alice", 4999, "key-1"))

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

def test_new_attempt_blocked_while_one_is_pending(conn, new_cart):
    cart_id = new_cart()
    new_payment(conn, cart_id, key="key-1")
    assert create_payment(conn, cart_id, "alice", 4999, "key-2") is None


def test_new_attempt_allowed_after_a_decline(conn, new_cart):
    cart_id = new_cart()
    first = new_payment(conn, cart_id, key="key-1")
    assert mark_declined(conn, first) is True
    second = new_payment(conn, cart_id, key="key-2")
    assert second != first


# --- The arrows ---

def test_approve_then_capture(conn, new_cart):
    cart_id = new_cart()
    p = new_payment(conn, cart_id)
    assert mark_approved(conn, p, "bank-123") is True
    assert mark_captured(conn, p) is True
    assert conn.execute("SELECT status, bank_id FROM payments WHERE id = %s", (p,)).fetchone() == (
        "captured",
        "bank-123",
    )


def test_cannot_capture_before_approval(conn, new_cart):
    cart_id = new_cart()
    p = new_payment(conn, cart_id)
    assert mark_captured(conn, p) is False
    assert payment_status(conn, p) == "pending"


def test_approved_payment_can_be_voided(conn, new_cart):
    cart_id = new_cart()
    # Alice lost the seat after the bank approved: release the held money.
    p = new_payment(conn, cart_id)
    mark_approved(conn, p, "bank-123")
    assert mark_voided(conn, p) is True
    assert payment_status(conn, p) == "voided"


def test_final_states_never_change(conn, new_cart):
    cart_id = new_cart()
    declined = new_payment(conn, cart_id, key="key-1")
    mark_declined(conn, declined)
    voided = new_payment(conn, cart_id, key="key-2")
    mark_voided(conn, voided)
    captured = new_payment(conn, cart_id, key="key-3")
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

def test_background_job_voids_only_old_pending_payments(conn, new_cart):
    cart_id = new_cart()
    old = new_payment(conn, cart_id, user_id="alice", key="key-1")
    conn.execute("UPDATE payments SET created_at = now() - interval '6 minutes' WHERE id = %s", (old,))
    conn.commit()
    fresh = new_payment(conn, new_cart("bob"), user_id="bob", key="key-2")
    approved = new_payment(conn, new_cart("carol"), user_id="carol", key="key-3")
    conn.execute("UPDATE payments SET created_at = now() - interval '6 minutes' WHERE id = %s", (approved,))
    conn.commit()
    mark_approved(conn, approved, "bank-123")

    assert void_stuck_payments(conn) == [old]
    assert payment_status(conn, old) == "voided"
    assert payment_status(conn, fresh) == "pending"
    assert payment_status(conn, approved) == "approved"


def test_approval_after_the_job_voided_it_is_rejected(conn, new_cart):
    cart_id = new_cart()
    p = new_payment(conn, cart_id)
    assert void_stuck_payments(conn, max_age_seconds=0) == [p]
    # The bank's "yes" finally arrives. It loses: the caller must void it at the bank.
    assert mark_approved(conn, p, "bank-123") is False
    assert payment_status(conn, p) == "voided"
