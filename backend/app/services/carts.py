from dataclasses import dataclass, field

import psycopg

from app.config import settings

# A cart is up to 3 seats that one buyer holds and pays for together. The cart row keeps
# the deadline and the payment attempts. Each of its seats keeps a COPY of the deadline in
# hold_expires_at, so CLAIM_SQL never has to look at another table. The cart's deadline and
# its seats' copies are always changed in the same transaction.
#
# Everything that changes a buyer's cart first locks the buyer's row in users (FOR UPDATE).
# One buyer's requests then take turns: a seat can't be added while Pay is starting, and
# 4 tabs clicking at once can't all pass the ticket limit.
#
# Seats are always locked in id order. If one request locked seat 2 and waited for seat 1
# while another held seat 1 and waited for seat 2, both would wait forever (a deadlock).
# A statement that changes several seats therefore picks them through a sub-select that
# locks them in id order first (ORDER BY id FOR UPDATE); without it, Postgres may visit
# the rows in the order they're stored on disk.

TICKET_LIMIT = 3

# Check and change in ONE statement: the WHERE clause is the "only if it's
# still free" condition, so two buyers can never both match the same seat.
# A seat is claimable if it's open, or if someone's hold or payment window has expired.
# now() is the database's clock, so every server agrees on what "expired" means.
CLAIM_SQL = """
UPDATE seats
SET status = 'held',
    held_by = %(user_id)s,
    cart_id = %(cart_id)s,
    hold_expires_at = %(expires_at)s
WHERE id = %(seat_id)s
  AND event_id = %(event_id)s
  AND (status = 'open'
       OR (status IN ('held', 'payment_pending') AND hold_expires_at < now()))
"""

# Seats this buyer has in the event: sold, or held/paying with time left.
# Expired holds don't count, matching CLAIM_SQL's idea of "taken".
COUNT_BUYER_SEATS_SQL = """
SELECT count(*) FROM seats
WHERE held_by = %(user_id)s
  AND event_id = %(event_id)s
  AND (status = 'sold' OR (status IN ('held', 'payment_pending') AND hold_expires_at > now()))
"""

# The buyer's cart that still has time left, if any.
ACTIVE_CART_SQL = """
SELECT cart_id FROM seats
WHERE held_by = %(user_id)s
  AND event_id = %(event_id)s
  AND status IN ('held', 'payment_pending')
  AND hold_expires_at > now()
LIMIT 1
"""


def _lock_buyer(cur: psycopg.Cursor, account_id: int) -> None:
    cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (account_id,))


@dataclass
class HoldResult:
    outcome: str  # 'held', 'taken' (none of the seats), 'limit_reached' or 'cart_active'
    cart_id: int | None = None
    held: list[int] = field(default_factory=list)
    taken: list[int] = field(default_factory=list)


def hold_seats(
    conn: psycopg.Connection,
    account_id: int,
    event_id: int,
    seat_ids: list[int],
    limit: int = TICKET_LIMIT,
    hold_seconds: int = settings.hold_seconds,
) -> HoldResult:
    """Start a cart: try to claim each seat and keep the ones we win.
    Seats someone else got are listed in `taken`, so the buyer can pick others."""
    user_id = str(account_id)
    seat_ids = sorted(set(seat_ids))  # claimed one by one in id order (see the top of this file)
    params = {"user_id": user_id, "event_id": event_id}
    with conn.cursor() as cur:
        _lock_buyer(cur, account_id)
        cur.execute(ACTIVE_CART_SQL, params)
        if cur.fetchone() is not None:
            conn.rollback()  # releases the lock
            return HoldResult("cart_active")
        cur.execute(COUNT_BUYER_SEATS_SQL, params)
        if cur.fetchone()[0] + len(seat_ids) > limit:
            conn.rollback()
            return HoldResult("limit_reached")

        cur.execute(
            "INSERT INTO carts (user_id, event_id, expires_at) "
            "VALUES (%s, %s, now() + make_interval(secs => %s)) RETURNING id, expires_at",
            (user_id, event_id, hold_seconds),
        )
        cart_id, expires_at = cur.fetchone()
        held, taken = [], []
        for seat_id in seat_ids:
            cur.execute(CLAIM_SQL, {**params, "seat_id": seat_id, "cart_id": cart_id, "expires_at": expires_at})
            (held if cur.rowcount == 1 else taken).append(seat_id)  # 1 row changed = we won it

        if not held:
            conn.rollback()  # no empty carts
            return HoldResult("taken", taken=taken)
    conn.commit()  # releases the locks; the next buyer now sees these seats as held
    return HoldResult("held", cart_id, held, taken)


# The buyer's own cart: its event, its deadline, whether it still has time left,
# and whether payment has started (then the cart is locked).
CART_SQL = """
SELECT event_id, expires_at,
       expires_at > now() AND EXISTS (
           SELECT 1 FROM seats
           WHERE cart_id = carts.id AND held_by = carts.user_id AND status IN ('held', 'payment_pending')),
       EXISTS (
           SELECT 1 FROM seats
           WHERE cart_id = carts.id AND held_by = carts.user_id AND status = 'payment_pending')
FROM carts
WHERE id = %(cart_id)s AND user_id = %(user_id)s
"""


def add_seat(
    conn: psycopg.Connection,
    account_id: int,
    cart_id: int,
    seat_id: int,
    limit: int = TICKET_LIMIT,
) -> str:
    """Add a replacement seat to a cart. Returns 'held', 'taken', 'limit_reached',
    'cart_locked' (payment started), 'cart_expired' or 'not_found'.

    The new seat ends at the cart's deadline, not a fresh 5 minutes: otherwise swapping
    seats over and over would keep the others held forever."""
    user_id = str(account_id)
    params = {"user_id": user_id, "cart_id": cart_id}
    with conn.cursor() as cur:
        _lock_buyer(cur, account_id)
        cur.execute(CART_SQL, params)
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return "not_found"
        event_id, expires_at, has_time_left, paying = row
        if not has_time_left:
            conn.rollback()
            return "cart_expired"
        if paying:
            conn.rollback()
            return "cart_locked"
        cur.execute(COUNT_BUYER_SEATS_SQL, {**params, "event_id": event_id})
        if cur.fetchone()[0] >= limit:
            conn.rollback()
            return "limit_reached"
        cur.execute(
            CLAIM_SQL,
            {**params, "event_id": event_id, "seat_id": seat_id, "expires_at": expires_at},
        )
        won = cur.rowcount == 1
    conn.commit()
    return "held" if won else "taken"


@dataclass
class StartResult:
    outcome: str  # 'started', 'expired', 'cart_changed' or 'not_holder'
    expired: list[int] = field(default_factory=list)  # seats on screen that are no longer held


# Only the seats the buyer sees on screen, only if they're still in this buyer's cart
# with time left. Moving to 'payment_pending' keeps them out of reach of CLAIM_SQL
# until the payment deadline passes.
START_PAYMENT_SQL = """
UPDATE seats
SET status = 'payment_pending',
    hold_expires_at = now() + make_interval(secs => %(payment_seconds)s)
FROM (SELECT id FROM seats WHERE id = ANY(%(seat_ids)s) ORDER BY id FOR UPDATE) AS locked
WHERE seats.id = locked.id
  AND seats.cart_id = %(cart_id)s
  AND seats.held_by = %(user_id)s
  AND seats.status = 'held'
  AND seats.hold_expires_at > now()
"""

HELD_SEATS_SQL = """
SELECT id FROM seats
WHERE cart_id = %(cart_id)s AND held_by = %(user_id)s AND status = 'held' AND hold_expires_at > now()
"""


def start_payment(
    conn: psycopg.Connection,
    account_id: int,
    cart_id: int,
    seat_ids: list[int],
    payment_seconds: int = settings.payment_seconds,
) -> StartResult:
    """Move every seat on the buyer's screen to 'payment_pending', or none of them.

    Success = the UPDATE moved exactly len(seat_ids) seats. The count comes from the
    request, not from the database: if a lost seat had left the cart, a database count
    would shrink to match and "succeed" with fewer seats than the buyer saw."""
    user_id = str(account_id)
    seat_ids = sorted(set(seat_ids))
    params = {"user_id": user_id, "cart_id": cart_id, "seat_ids": seat_ids, "payment_seconds": payment_seconds}
    with conn.cursor() as cur:
        _lock_buyer(cur, account_id)
        cur.execute(HELD_SEATS_SQL, params)
        held = {row[0] for row in cur.fetchall()}  # only to explain a failure; the UPDATE decides
        cur.execute(START_PAYMENT_SQL, params)
        if cur.rowcount != len(seat_ids):
            conn.rollback()  # the seats that did move go back to 'held'
            if not held:
                return StartResult("not_holder")
            return StartResult("expired", [s for s in seat_ids if s not in held])
        if held - set(seat_ids):
            conn.rollback()  # the cart has seats the buyer's screen doesn't show
            return StartResult("cart_changed")
        # now() is fixed for the whole transaction, so the cart gets exactly the
        # deadline its seats just got.
        cur.execute(
            "UPDATE carts SET expires_at = now() + make_interval(secs => %(payment_seconds)s) "
            "WHERE id = %(cart_id)s",
            params,
        )
    conn.commit()
    return StartResult("started")


# The bank approved. Only sell if the payment window is still open; a late
# approval returns False and the caller must void it, even if nobody has
# claimed the seats since.
CONFIRM_PAYMENT_SQL = """
UPDATE seats
SET status = 'sold',
    hold_expires_at = NULL
FROM (SELECT id FROM seats WHERE id = ANY(%(seat_ids)s) ORDER BY id FOR UPDATE) AS locked
WHERE seats.id = locked.id
  AND seats.cart_id = %(cart_id)s
  AND seats.held_by = %(user_id)s
  AND seats.status = 'payment_pending'
  AND seats.hold_expires_at > now()
"""


def confirm_payment(conn: psycopg.Connection, account_id: int, cart_id: int, seat_ids: list[int]) -> bool:
    """Mark every paid seat sold, or none. False means the payment arrived too late: void it."""
    seat_ids = sorted(set(seat_ids))
    with conn.cursor() as cur:
        cur.execute(CONFIRM_PAYMENT_SQL, {"seat_ids": seat_ids, "cart_id": cart_id, "user_id": str(account_id)})
        if cur.rowcount != len(seat_ids):
            conn.rollback()
            return False
    conn.commit()
    return True


# The bank declined. Count the attempt for the whole cart. RETURNING sees the
# value AFTER this update.
DECLINE_PAYMENT_SQL = """
UPDATE carts
SET payment_attempts = payment_attempts + 1
WHERE id = %(cart_id)s
  AND user_id = %(user_id)s
  AND expires_at > now()
  AND payment_attempts < %(max_attempts)s
RETURNING %(max_attempts)s - payment_attempts
"""

# The last allowed decline: every seat in the cart goes back on sale.
RELEASE_SEATS_SQL = """
UPDATE seats
SET status = 'open', held_by = NULL, cart_id = NULL, hold_expires_at = NULL
FROM (SELECT id FROM seats WHERE cart_id = %(cart_id)s ORDER BY id FOR UPDATE) AS locked
WHERE seats.id = locked.id AND seats.held_by = %(user_id)s AND seats.status = 'payment_pending'
"""


def record_declined_payment(
    conn: psycopg.Connection,
    account_id: int,
    cart_id: int,
    max_attempts: int = settings.max_payment_attempts,
) -> int | None:
    """Returns attempts left (0 = seats released), or None if the payment window is closed."""
    params = {"cart_id": cart_id, "user_id": str(account_id), "max_attempts": max_attempts}
    with conn.cursor() as cur:
        cur.execute(DECLINE_PAYMENT_SQL, params)
        row = cur.fetchone()
        if row is not None and row[0] == 0:
            cur.execute(RELEASE_SEATS_SQL, params)
    conn.commit()
    return None if row is None else row[0]


def paying_seats(conn: psycopg.Connection, account_id: int, cart_id: int) -> set[int]:
    """Seats in this cart with payment started and time left. Read-only: on a retry it
    tells checkout that payment already started; confirm_payment still makes the real decision."""
    rows = conn.execute(
        "SELECT id FROM seats WHERE cart_id = %s AND held_by = %s "
        "AND status = 'payment_pending' AND hold_expires_at > now()",
        (cart_id, str(account_id)),
    ).fetchall()
    conn.commit()
    return {row[0] for row in rows}


def price(conn: psycopg.Connection, seat_ids: list[int]) -> int:
    """What we charge. Always read from the database, never taken from the request."""
    row = conn.execute(
        "SELECT COALESCE(sum(price_cents), 0) FROM seats WHERE id = ANY(%s)", (sorted(set(seat_ids)),)
    ).fetchone()
    conn.commit()
    return row[0]


def seconds_left(conn: psycopg.Connection, account_id: int, cart_id: int) -> int | None:
    """Seconds left on this buyer's cart, by the database clock. None = nothing held anymore.
    Sent as a duration so a wrong clock on the buyer's computer can't skew their countdown."""
    row = conn.execute(
        "SELECT ceil(extract(epoch FROM expires_at - now()))::int FROM carts "
        "WHERE id = %(cart_id)s AND user_id = %(user_id)s AND expires_at > now() "
        "AND EXISTS (SELECT 1 FROM seats WHERE cart_id = carts.id AND held_by = carts.user_id "
        "            AND status IN ('held', 'payment_pending'))",
        {"cart_id": cart_id, "user_id": str(account_id)},
    ).fetchone()
    conn.commit()
    return None if row is None else row[0]


def seats_event_id(conn: psycopg.Connection, seat_ids: list[int]) -> int | None:
    """The event these seats belong to, or None if any is unknown or they're from different events."""
    seat_ids = sorted(set(seat_ids))
    rows = conn.execute("SELECT DISTINCT event_id FROM seats WHERE id = ANY(%s)", (seat_ids,)).fetchall()
    found = conn.execute("SELECT count(*) FROM seats WHERE id = ANY(%s)", (seat_ids,)).fetchone()[0]
    conn.commit()
    return rows[0][0] if len(rows) == 1 and found == len(seat_ids) else None


def cart_event_id(conn: psycopg.Connection, cart_id: int) -> int | None:
    row = conn.execute("SELECT event_id FROM carts WHERE id = %s", (cart_id,)).fetchone()
    conn.commit()
    return None if row is None else row[0]
