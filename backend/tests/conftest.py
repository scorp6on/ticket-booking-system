from pathlib import Path

import psycopg
import pytest

from app.config import settings

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
        "INSERT INTO seats (event_id, label) VALUES (%s, '14B') RETURNING id", (event_id,)
    ).fetchone()[0]
    conn.commit()
    return sid
