from pathlib import Path

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from app.api.routes import get_bank
from app.config import settings
from app.main import app
from app.services import accounts, queue
from app.services.bank import MockBank

SCHEMA = (Path(__file__).parent.parent / "app" / "schema.sql").read_text()


@pytest.fixture
def conn():
    with psycopg.connect(settings.database_url) as c:
        # Rebuild from scratch so schema changes always apply (wipes local data).
        c.execute("DROP TABLE IF EXISTS payments, seats, carts, events, sessions, users")
        c.execute(SCHEMA)
        c.commit()
        yield c


@pytest.fixture
def seat_id(conn):
    """One open seat, '14B', for a test event."""
    event_id = conn.execute(
        "INSERT INTO events (name, starts_at) VALUES ('Test Show', now() + interval '1 day') RETURNING id"
    ).fetchone()[0]
    sid = conn.execute(
        "INSERT INTO seats (event_id, label, price_cents) VALUES (%s, '14B', 4999) RETURNING id", (event_id,)
    ).fetchone()[0]
    conn.commit()
    return sid


@pytest.fixture
def r():
    """Redis, emptied before each test (wipes the local queue data)."""
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    client.flushdb()
    yield client
    client.close()


@pytest.fixture
def event_id(conn, seat_id):
    return conn.execute("SELECT event_id FROM seats WHERE id = %s", (seat_id,)).fetchone()[0]


@pytest.fixture
def bank():
    return MockBank(replies=[])


@pytest.fixture
def client(conn, r, bank):  # conn and r first: they wipe Postgres and Redis
    app.dependency_overrides[get_bank] = lambda: bank
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def buyer(conn):
    """Verified, logged-in accounts by name: buyer("alice") -> (account_id, headers with her session cookie)."""
    made = {}

    def get(name):
        if name not in made:
            email = f"{name}@example.com"
            account_id = accounts.sign_up(conn, email, "correct-horse")
            accounts.verify_id(conn, account_id, f"ID-{name}")
            made[name] = (account_id, {"Cookie": f"session={accounts.log_in(conn, email, 'correct-horse')}"})
        return made[name]

    return get


@pytest.fixture
def admitted(event_id, r, buyer):
    """Headers for a buyer who waited their turn and holds a valid queue token."""
    def headers(name):
        account_id, cookie = buyer(name)
        expires_ms = (r.time()[0] + 300) * 1000
        return {**cookie, "X-Queue-Token": queue.issue_token(event_id, str(account_id), expires_ms)}
    return headers


@pytest.fixture
def paying(buyer):
    """Headers for a buyer pressing Pay: their session cookie plus an idempotency key."""
    def headers(name, key=None):
        h = dict(buyer(name)[1])
        if key:
            h["Idempotency-Key"] = key
        return h
    return headers


@pytest.fixture
def account(conn):
    """User rows by name, skipping the slow password hashing: account("alice") -> account id."""
    made = {}

    def get(name):
        if name not in made:
            made[name] = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, 'not-a-real-hash') RETURNING id",
                (f"{name}@example.com",),
            ).fetchone()[0]
            conn.commit()
        return made[name]

    return get


@pytest.fixture
def new_cart(conn, event_id):
    """A bare cart row, for tests of payments alone: new_cart("alice") -> cart id."""
    def make(user_id="alice"):
        cart_id = conn.execute(
            "INSERT INTO carts (user_id, event_id, expires_at) VALUES (%s, %s, now() + interval '5 minutes') "
            "RETURNING id",
            (user_id, event_id),
        ).fetchone()[0]
        conn.commit()
        return cart_id
    return make


@pytest.fixture
def seats(conn, event_id, seat_id):
    """Six open seats in the test event: '14B' plus A1-A5."""
    for n in range(1, 6):
        conn.execute("INSERT INTO seats (event_id, label, price_cents) VALUES (%s, %s, 4999)", (event_id, f"A{n}"))
    conn.commit()
    return [r[0] for r in conn.execute("SELECT id FROM seats WHERE event_id = %s ORDER BY id", (event_id,))]
