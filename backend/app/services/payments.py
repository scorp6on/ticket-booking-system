import psycopg

from app.config import settings

# Payment states (one row per press of "Pay"):
#   pending  -> approved | declined | voided
#   approved -> captured | voided
#   declined, voided, captured are final.
# Every arrow is one conditional UPDATE: the WHERE names the state we expect
# to move FROM, so if something else moved the payment first, we get 0 rows.

# ON CONFLICT DO NOTHING covers both unique rules: a repeated idempotency key,
# and a second pending payment from the same buyer for the same seat.
CREATE_PAYMENT_SQL = """
INSERT INTO payments (seat_id, user_id, amount_cents, idempotency_key)
VALUES (%(seat_id)s, %(user_id)s, %(amount_cents)s, %(key)s)
ON CONFLICT DO NOTHING
RETURNING id
"""


def create_payment(
    conn: psycopg.Connection,
    seat_id: int,
    user_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> tuple[int, bool] | None:
    """Record a new payment attempt.

    Returns (payment_id, True) if this is a new attempt: go ask the bank.
    Returns (payment_id, False) if this key was seen before: return the saved result, don't ask again.
    Returns None if another payment for this seat is still waiting on the bank.
    """
    with conn.cursor() as cur:
        cur.execute(
            CREATE_PAYMENT_SQL,
            {"seat_id": seat_id, "user_id": user_id, "amount_cents": amount_cents, "key": idempotency_key},
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT id FROM payments WHERE idempotency_key = %s", (idempotency_key,))
            existing = cur.fetchone()
    conn.commit()
    if row is not None:
        return row[0], True
    return None if existing is None else (existing[0], False)


def _move(conn: psycopg.Connection, sql: str, params: dict) -> bool:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        moved = cur.rowcount == 1
    conn.commit()
    return moved


def mark_approved(conn: psycopg.Connection, payment_id: int, bank_id: str) -> bool:
    """False means the payment was already voided: this is a late approval, void it at the bank."""
    return _move(
        conn,
        "UPDATE payments SET status = 'approved', bank_id = %(bank_id)s "
        "WHERE id = %(id)s AND status = 'pending'",
        {"id": payment_id, "bank_id": bank_id},
    )


def mark_declined(conn: psycopg.Connection, payment_id: int) -> bool:
    return _move(
        conn,
        "UPDATE payments SET status = 'declined' WHERE id = %(id)s AND status = 'pending'",
        {"id": payment_id},
    )


def mark_captured(conn: psycopg.Connection, payment_id: int) -> bool:
    return _move(
        conn,
        "UPDATE payments SET status = 'captured' WHERE id = %(id)s AND status = 'approved'",
        {"id": payment_id},
    )


def mark_voided(conn: psycopg.Connection, payment_id: int) -> bool:
    return _move(
        conn,
        "UPDATE payments SET status = 'voided' WHERE id = %(id)s AND status IN ('pending', 'approved')",
        {"id": payment_id},
    )


# The background job: payments the bank never answered within the payment window.
# Marking them voided here is what makes a later approval lose the race in mark_approved.
VOID_STUCK_SQL = """
UPDATE payments
SET status = 'voided'
WHERE status = 'pending'
  AND created_at < now() - make_interval(secs => %(max_age_seconds)s)
RETURNING id
"""


def void_stuck_payments(
    conn: psycopg.Connection,
    max_age_seconds: int = settings.payment_seconds,
) -> list[int]:
    """Void payments with no bank reply. Returns their ids so the caller can cancel them at the bank."""
    with conn.cursor() as cur:
        cur.execute(VOID_STUCK_SQL, {"max_age_seconds": max_age_seconds})
        ids = [row[0] for row in cur.fetchall()]
    conn.commit()
    return ids
