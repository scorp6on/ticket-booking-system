from pathlib import Path

import psycopg

from app.config import settings

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
ROWS = "ABCDE"
SEATS_PER_ROW = 8


def create_event(conn: psycopg.Connection, name: str, opens_in_seconds: int) -> int:
    """Create the tables if needed and add an event with rows A-E x 8 seats."""
    conn.execute(SCHEMA)
    event_id = conn.execute(
        "INSERT INTO events (name, starts_at, sale_opens_at) "
        "VALUES (%s, now() + interval '7 days', now() + make_interval(secs => %s)) RETURNING id",
        (name, opens_in_seconds),
    ).fetchone()[0]
    for i, row in enumerate(ROWS):
        price = 7999 if i < 2 else 4999  # front rows cost more
        for n in range(1, SEATS_PER_ROW + 1):
            conn.execute(
                "INSERT INTO seats (event_id, label, price_cents) VALUES (%s, %s, %s)",
                (event_id, f"{row}{n}", price),
            )
    conn.commit()
    return event_id


def main() -> None:
    """Add a demo event. Running the tests wipes this; run it again after."""
    with psycopg.connect(settings.database_url) as conn:
        event_id = create_event(conn, "Demo Concert", 60)
    print(f"Created event {event_id}, sale opens in 1 minute: http://localhost:5173/?event={event_id}")


if __name__ == "__main__":
    main()
