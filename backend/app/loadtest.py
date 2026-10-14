"""Load test helpers (the buyers themselves are in loadtest/buyers.js, run by k6).

    python -m app.loadtest setup 300      # 300 verified, logged-in accounts + a fresh 40-seat event
    python -m app.loadtest check <event>  # after k6 finishes: do the database rows add up?
"""
import hashlib
import json
import secrets
import sys
from pathlib import Path

import psycopg
import redis

from app.config import settings
from app.seed import create_event
from app.services.accounts import hasher

PASSWORD = "loadtest-password"
OPENS_IN_SECONDS = 30  # buyers who join before this get a random place, like real early arrivals
SESSIONS_FILE = Path(__file__).parents[2] / "loadtest" / "sessions.json"  # read by buyers.js; gitignored


def setup(buyers: int) -> None:
    # Accounts and logins go straight into the database. Real fans log in before the
    # sale, and 300 Argon2 checks in the same second would only measure the laptop's
    # CPU, not whether seats are sold correctly. One hash is reused for every account
    # (same password) to keep setup fast.
    password_hash = hasher.hash(PASSWORD)
    with psycopg.connect(settings.database_url) as conn:
        event_id = create_event(conn, "Load Test", OPENS_IN_SECONDS)
        for n in range(1, buyers + 1):
            conn.execute(
                "INSERT INTO users (email, password_hash, id_document_hash, id_verified_at) "
                "VALUES (%s, %s, %s, now()) ON CONFLICT (email) DO NOTHING",
                (f"loadtest-{n}@example.com", password_hash, hashlib.sha256(f"LOADTEST-{n}".encode()).hexdigest()),
            )
        # Log everyone in, the way log_in does: the buyer keeps the token, we keep its hash.
        tokens = []
        for n in range(1, buyers + 1):
            token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) "
                "SELECT %s, id, now() + interval '1 day' FROM users WHERE email = %s",
                (hashlib.sha256(token.encode()).hexdigest(), f"loadtest-{n}@example.com"),
            )
            tokens.append(token)
        conn.commit()
    SESSIONS_FILE.write_text(json.dumps(tokens))
    # Running the tests wipes Postgres, so event ids start over, but queue data the
    # tests left in Redis under the same id would otherwise mix into this sale.
    r = redis.Redis.from_url(settings.redis_url)
    r.delete(f"queue:{event_id}", f"room:{event_id}", f"seen:{event_id}")
    r.close()
    print(f"{buyers} verified, logged-in buyers ready. Event {event_id} opens in {OPENS_IN_SECONDS}s. Start k6 now, from the repo root:")
    print(f"  k6 run -e EVENT_ID={event_id} -e BUYERS={buyers} loadtest/buyers.js")


# Each query lists the rows that break a rule. A passing test finds none.
CHECKS = {
    "Every sold seat has exactly one captured payment (no double-sale, no free seat)": """
        SELECT s.label FROM seats s
        WHERE s.event_id = %(event)s AND s.status = 'sold'
          AND (SELECT count(*) FROM payments p WHERE p.seat_id = s.id AND p.status = 'captured') <> 1
    """,
    "No charge without a seat": """
        SELECT p.id FROM payments p JOIN seats s ON s.id = p.seat_id
        WHERE s.event_id = %(event)s AND p.status = 'captured' AND s.status <> 'sold'
    """,
    "The person who paid is the person who got the seat": """
        SELECT p.id FROM payments p JOIN seats s ON s.id = p.seat_id
        WHERE s.event_id = %(event)s AND p.status = 'captured' AND p.user_id IS DISTINCT FROM s.held_by
    """,
    "Charged the seat's price": """
        SELECT p.id FROM payments p JOIN seats s ON s.id = p.seat_id
        WHERE s.event_id = %(event)s AND p.status = 'captured' AND p.amount_cents <> s.price_cents
    """,
    "No buyer has more than 3 seats": """
        SELECT held_by FROM seats
        WHERE event_id = %(event)s AND status = 'sold'
        GROUP BY held_by HAVING count(*) > 3
    """,
    "No stuck payments (pending or approved-but-not-captured)": """
        SELECT p.id FROM payments p JOIN seats s ON s.id = p.seat_id
        WHERE s.event_id = %(event)s AND p.status IN ('pending', 'approved')
    """,
}


def check(event_id: int) -> bool:
    params = {"event": event_id}
    with psycopg.connect(settings.database_url) as conn:
        sold, total, buyers = conn.execute(
            "SELECT count(*) FILTER (WHERE status = 'sold'), count(*), count(DISTINCT held_by) FILTER (WHERE status = 'sold') "
            "FROM seats WHERE event_id = %(event)s",
            params,
        ).fetchone()
        payments = conn.execute(
            "SELECT p.status, count(*) FROM payments p JOIN seats s ON s.id = p.seat_id "
            "WHERE s.event_id = %(event)s GROUP BY p.status ORDER BY p.status",
            params,
        ).fetchall()
        print(f"Event {event_id}: {sold}/{total} seats sold to {buyers} buyers")
        print("Payments: " + (", ".join(f"{n} {status}" for status, n in payments) or "none"))
        print()

        all_passed = True
        for rule, sql in CHECKS.items():
            bad = conn.execute(sql, params).fetchall()
            all_passed &= not bad
            print(f"{'PASS' if not bad else 'FAIL'}  {rule}" + (f"  -> {[row[0] for row in bad]}" if bad else ""))
    return all_passed


if __name__ == "__main__":
    command, arg = sys.argv[1], int(sys.argv[2])
    if command == "setup":
        setup(arg)
    else:
        sys.exit(0 if check(arg) else 1)
