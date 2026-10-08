import psycopg

from app.config import settings

# Check and change in ONE statement: the WHERE clause is the "only if it's
# still free" condition, so two buyers can never both match the same seat.
# A seat is claimable if it's open, or if someone's hold or payment window has expired.
# now() is the database's clock, so every server agrees on what "expired" means.
CLAIM_SQL = """
UPDATE seats
SET status = 'held',
    held_by = %(user_id)s,
    hold_expires_at = now() + make_interval(secs => %(hold_seconds)s),
    payment_attempts = 0
WHERE id = %(seat_id)s
  AND (status = 'open'
       OR (status IN ('held', 'payment_pending') AND hold_expires_at < now()))
"""


def claim_seat(
    conn: psycopg.Connection,
    seat_id: int,
    user_id: str,
    hold_seconds: int = settings.hold_seconds,
) -> bool:
    """Try to hold a seat. Returns True if this user won it, False if it's taken."""
    with conn.cursor() as cur:
        cur.execute(
            CLAIM_SQL,
            {"seat_id": seat_id, "user_id": user_id, "hold_seconds": hold_seconds},
        )
        won = cur.rowcount == 1  # 1 row changed = we won; 0 = someone else has it
    conn.commit()  # release the row lock right away so the next buyer isn't kept waiting
    return won


# Only the person holding the seat, and only while their hold is still valid.
# Moving to 'payment_pending' keeps the seat out of reach of CLAIM_SQL until
# the payment deadline passes.
START_PAYMENT_SQL = """
UPDATE seats
SET status = 'payment_pending',
    hold_expires_at = now() + make_interval(secs => %(payment_seconds)s)
WHERE id = %(seat_id)s
  AND status = 'held'
  AND held_by = %(user_id)s
  AND hold_expires_at > now()
"""


def start_payment(
    conn: psycopg.Connection,
    seat_id: int,
    user_id: str,
    payment_seconds: int = settings.payment_seconds,
) -> bool:
    """Lock the seat for payment. Returns False if this user doesn't hold a valid hold on it."""
    with conn.cursor() as cur:
        cur.execute(
            START_PAYMENT_SQL,
            {"seat_id": seat_id, "user_id": user_id, "payment_seconds": payment_seconds},
        )
        ok = cur.rowcount == 1
    conn.commit()
    return ok


# The bank approved. Only sell if the payment window is still open; a late
# approval returns False and the caller must void it, even if nobody has
# claimed the seat since.
CONFIRM_PAYMENT_SQL = """
UPDATE seats
SET status = 'sold',
    hold_expires_at = NULL
WHERE id = %(seat_id)s
  AND status = 'payment_pending'
  AND held_by = %(user_id)s
  AND hold_expires_at > now()
"""


def confirm_payment(conn: psycopg.Connection, seat_id: int, user_id: str) -> bool:
    """Mark the seat sold. Returns False if the payment arrived too late or isn't theirs: void it."""
    with conn.cursor() as cur:
        cur.execute(CONFIRM_PAYMENT_SQL, {"seat_id": seat_id, "user_id": user_id})
        sold = cur.rowcount == 1
    conn.commit()
    return sold


# The bank declined. Count the attempt; on the last allowed one, put the seat
# back on sale. Inside SET, payment_attempts means the value BEFORE this update.
DECLINE_PAYMENT_SQL = """
UPDATE seats
SET payment_attempts = payment_attempts + 1,
    status          = CASE WHEN payment_attempts + 1 >= %(max_attempts)s THEN 'open' ELSE status END,
    held_by         = CASE WHEN payment_attempts + 1 >= %(max_attempts)s THEN NULL ELSE held_by END,
    hold_expires_at = CASE WHEN payment_attempts + 1 >= %(max_attempts)s THEN NULL ELSE hold_expires_at END
WHERE id = %(seat_id)s
  AND status = 'payment_pending'
  AND held_by = %(user_id)s
  AND hold_expires_at > now()
RETURNING %(max_attempts)s - payment_attempts
"""


def record_declined_payment(
    conn: psycopg.Connection,
    seat_id: int,
    user_id: str,
    max_attempts: int = settings.max_payment_attempts,
) -> int | None:
    """Returns attempts left (0 = seat released), or None if this user has no valid payment on the seat."""
    with conn.cursor() as cur:
        cur.execute(
            DECLINE_PAYMENT_SQL,
            {"seat_id": seat_id, "user_id": user_id, "max_attempts": max_attempts},
        )
        row = cur.fetchone()
    conn.commit()
    return None if row is None else row[0]


def in_payment_window(conn: psycopg.Connection, seat_id: int, user_id: str) -> bool:
    """Read-only check for retries: is this user's payment window still open?
    Only used to skip a pointless bank call; confirm_payment still makes the real decision."""
    row = conn.execute(
        "SELECT 1 FROM seats WHERE id = %s AND status = 'payment_pending' "
        "AND held_by = %s AND hold_expires_at > now()",
        (seat_id, user_id),
    ).fetchone()
    conn.commit()
    return row is not None
