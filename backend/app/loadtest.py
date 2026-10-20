"""Load test helpers (the buyers and bots themselves are in loadtest/*.js, run by k6).

    python -m app.loadtest setup 300      # 300 verified, logged-in fans, the bots, a fresh 40-seat event
    python -m app.loadtest check <event>  # after k6 finishes: do the database rows add up?
    python -m app.loadtest bot-test 300   # fans only, then fans + bots, compared; one verdict
"""
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import redis

from app.config import settings
from app.seed import create_event
from app.services.accounts import hasher

PASSWORD = "loadtest-password"
OPENS_IN_SECONDS = 30  # buyers who join before this get a random place, like real early arrivals
LOADTEST_DIR = Path(__file__).parents[2] / "loadtest"
SESSIONS_FILE = LOADTEST_DIR / "sessions.json"  # fans' session tokens, read by buyers.js; gitignored
BOTS_FILE = LOADTEST_DIR / "bots.json"  # bots' session tokens and ids, read by attack.js; gitignored

# Each kind of bot gets its own accounts (bot-<kind>-<n>@example.com), so the database
# shows exactly what each kind got. Every bot is verified and logged in: the strongest
# version of each attack.
BOTS_PER_KIND = 40
BOT_KINDS = ["skipper", "forger", "reuser", "hammerer"]


def _make_accounts(conn: psycopg.Connection, prefix: str, count: int, password_hash: str) -> list[dict]:
    """Verified accounts, already logged in. Returns each one's session token and id."""
    made = []
    for n in range(1, count + 1):
        email = f"{prefix}-{n}@example.com"
        conn.execute(
            "INSERT INTO users (email, password_hash, id_document_hash, id_verified_at) "
            "VALUES (%s, %s, %s, now()) ON CONFLICT (email) DO NOTHING",
            (email, password_hash, hashlib.sha256(f"{prefix}-{n}".upper().encode()).hexdigest()),
        )
        # Log in the way log_in does: the buyer keeps the token, we keep its hash.
        token = secrets.token_urlsafe(32)
        user_id = conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) "
            "SELECT %s, id, now() + interval '1 day' FROM users WHERE email = %s RETURNING user_id",
            (hashlib.sha256(token.encode()).hexdigest(), email),
        ).fetchone()[0]
        made.append({"token": token, "user_id": str(user_id)})
    return made


def setup(buyers: int) -> tuple[int, int]:
    """Returns (event id, when the sale opens in ms)."""
    # Accounts and logins go straight into the database. Real fans log in before the
    # sale, and 300 Argon2 checks in the same second would only measure the laptop's
    # CPU, not whether seats are sold correctly. One hash is reused for every account
    # (same password) to keep setup fast.
    password_hash = hasher.hash(PASSWORD)
    with psycopg.connect(settings.database_url) as conn:
        event_id = create_event(conn, "Load Test", OPENS_IN_SECONDS)
        opens_at_ms = conn.execute(
            "SELECT (extract(epoch FROM sale_opens_at) * 1000)::bigint FROM events WHERE id = %s", (event_id,)
        ).fetchone()[0]
        fans = _make_accounts(conn, "loadtest", buyers, password_hash)
        bots = {kind: _make_accounts(conn, f"bot-{kind}", BOTS_PER_KIND, password_hash) for kind in BOT_KINDS}
        bots["scalper"] = _make_accounts(conn, "bot-scalper", 1, password_hash)[0]  # one person, many tabs
        conn.commit()
    SESSIONS_FILE.write_text(json.dumps([f["token"] for f in fans]))
    BOTS_FILE.write_text(json.dumps(bots))
    # Running the tests wipes Postgres, so event ids start over, but queue data the
    # tests left in Redis under the same id would otherwise mix into this sale.
    r = redis.Redis.from_url(settings.redis_url)
    r.delete(f"queue:{event_id}", f"room:{event_id}", f"seen:{event_id}")
    r.close()
    print(f"{buyers} verified, logged-in fans and the bots are ready. Event {event_id} opens in {OPENS_IN_SECONDS}s.")
    print("Start ONE of these now, from the repo root:")
    print(f"  k6 run -e EVENT_ID={event_id} -e BUYERS={buyers} loadtest/buyers.js      # fans only")
    print(f"  k6 run -e EVENT_ID={event_id} -e BUYERS={buyers} -e OPENS_AT={opens_at_ms} loadtest/attack.js  # fans + bots")
    return event_id, opens_at_ms


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

# After attack.js. Any trace of a seat (held, paid for, or sold) counts as getting through.
BOT_TRACES_SQL = """
    SELECT s.label FROM seats s JOIN users u ON s.held_by = u.id::text
    WHERE s.event_id = %(event)s AND u.email LIKE %(bots)s
    UNION ALL
    SELECT 'payment ' || p.id FROM payments p JOIN seats s ON s.id = p.seat_id JOIN users u ON p.user_id = u.id::text
    WHERE s.event_id = %(event)s AND u.email LIKE %(bots)s
"""
BOT_CHECKS = {
    "Line-skipping bots got no seats": (BOT_TRACES_SQL, "bot-skipper-%"),
    "Token-forging bots got no seats": (BOT_TRACES_SQL, "bot-forger-%"),
    "Queue-hammering bots (arriving after the fans) got no seats": (BOT_TRACES_SQL, "bot-hammerer-%"),
    "The scalper's 20 tabs got at most 3 seats": ("""
        SELECT s.label FROM seats s JOIN users u ON s.held_by = u.id::text
        WHERE s.event_id = %(event)s AND s.status = 'sold' AND u.email LIKE %(bots)s
        OFFSET 3
    """, "bot-scalper-%"),
    "Every seat sold (bots didn't leave seats stuck)": ("""
        SELECT label FROM seats WHERE event_id = %(event)s AND status <> 'sold'
    """, None),
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

        print()
        print("Bots (only meaningful after attack.js):")
        for rule, (sql, bots) in BOT_CHECKS.items():
            bad = conn.execute(sql, {**params, "bots": bots}).fetchall()
            all_passed &= not bad
            print(f"{'PASS' if not bad else 'FAIL'}  {rule}" + (f"  -> {[row[0] for row in bad]}" if bad else ""))
    return all_passed


# --- Bot test: fans only, then fans + bots, on the same laptop minutes apart ---

ALLOWED_SLOWDOWN = 2  # fans may be at most 2x slower while bots attack


def _k6(script: str, summary: Path, extra: dict) -> int:
    """Run one k6 script from the repo root. Returns k6's exit code (99 = a threshold failed)."""
    k6 = shutil.which("k6") or r"C:\Program Files\k6\k6.exe"
    args = [k6, "run", "--quiet", "--log-output", "none", "--summary-export", str(summary)]
    for key, value in extra.items():
        args += ["-e", f"{key}={value}"]
    sys.stdout.flush()  # so our lines print before k6's, not after
    return subprocess.run([*args, f"loadtest/{script}"], cwd=LOADTEST_DIR.parent, env=os.environ).returncode


def bot_test(buyers: int) -> bool:
    # The baseline is measured right before the attack, not typed in: the laptop's speed
    # changes from day to day, and only the bots' effect should count.
    summary = LOADTEST_DIR / "summary.json"  # gitignored

    print("=== 1/2: fans only (the baseline) ===")
    event_id, _ = setup(buyers)
    _k6("buyers.js", summary, {"EVENT_ID": event_id, "BUYERS": buyers})
    fans_only = json.loads(summary.read_text())["metrics"]["http_req_duration"]
    fans_only_ok = check(event_id)

    print("\n=== 2/2: fans + bots ===")
    event_id, opens_at_ms = setup(buyers)
    k6_ok = _k6("attack.js", summary, {
        "EVENT_ID": event_id, "BUYERS": buyers, "OPENS_AT": opens_at_ms,
        "MAX_FAN_MED_MS": round(fans_only["med"] * ALLOWED_SLOWDOWN),
        "MAX_FAN_P95_MS": round(fans_only["p(95)"] * ALLOWED_SLOWDOWN),
    }) == 0
    with_bots = json.loads(summary.read_text())["metrics"]["http_req_duration{who:fan}"]
    attack_ok = check(event_id)

    print(f"\n=== Verdict (fans may be at most {ALLOWED_SLOWDOWN}x slower) ===")
    for name in ["med", "p(95)"]:
        before, after = fans_only[name], with_bots[name]
        print(f"Fans' {name:5}: {before:6.0f} ms alone, {after:6.0f} ms with bots ({after / before:.1f}x)")
    passed = fans_only_ok and attack_ok and k6_ok
    print("PASS" if passed else "FAIL (see the FAIL lines and k6's thresholds above)")
    return passed


if __name__ == "__main__":
    command, arg = sys.argv[1], int(sys.argv[2])
    if command == "setup":
        setup(arg)
    elif command == "bot-test":
        sys.exit(0 if bot_test(arg) else 1)
    else:
        sys.exit(0 if check(arg) else 1)
