from pathlib import Path

import psycopg

from app.config import settings

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
ROWS = "ABCDE"
SEATS_PER_ROW = 8


def main() -> None:
    """Create the tables if needed and add a demo event. Running the tests wipes this; run it again after."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(SCHEMA)
        event_id = conn.execute(
            "INSERT INTO events (name, starts_at, sale_opens_at) "
            "VALUES ('Demo Concert', now() + interval '7 days', now() + interval '1 minute') RETURNING id"
        ).fetchone()[0]
        for i, row in enumerate(ROWS):
            price = 7999 if i < 2 else 4999  # front rows cost more
            for n in range(1, SEATS_PER_ROW + 1):
                conn.execute(
                    "INSERT INTO seats (event_id, label, price_cents) VALUES (%s, %s, %s)",
                    (event_id, f"{row}{n}", price),
                )
        conn.commit()
    print(f"Created event {event_id}, sale opens in 1 minute: http://localhost:5173/?event={event_id}")


if __name__ == "__main__":
    main()
