from pathlib import Path

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from app.api.routes import get_bank
from app.config import settings
from app.main import app
from app.services import queue
from app.services.bank import MockBank

SCHEMA = (Path(__file__).parent.parent / "app" / "schema.sql").read_text()


@pytest.fixture
def conn():
    with psycopg.connect(settings.database_url) as c:
        # Rebuild from scratch so schema changes always apply (wipes local data).
        c.execute("DROP TABLE IF EXISTS payments, seats, events")
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
def admitted(event_id, r):
    """Headers for a user who waited their turn and holds a valid queue token."""
    def headers(user_id):
        expires_ms = (r.time()[0] + 300) * 1000
        return {"X-User-Id": user_id, "X-Queue-Token": queue.issue_token(event_id, user_id, expires_ms)}
    return headers
