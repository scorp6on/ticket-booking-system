import psycopg

# An expired hold is still 'held' in the table until someone claims the seat,
# so report it as 'open' here or buyers would see a free seat as taken.
# held_by is left out on purpose: buyers shouldn't see who holds a seat.
SEATS_SQL = """
SELECT id, label, price_cents,
       CASE WHEN status IN ('held', 'payment_pending') AND hold_expires_at < now()
            THEN 'open' ELSE status END
FROM seats
WHERE event_id = %s
ORDER BY id
"""


def get_event(conn: psycopg.Connection, event_id: int) -> dict | None:
    """The event and its seat map. The map is a hint: a seat can be taken a moment later."""
    event = conn.execute("SELECT id, name, starts_at FROM events WHERE id = %s", (event_id,)).fetchone()
    if event is None:
        conn.commit()
        return None
    seats = conn.execute(SEATS_SQL, (event_id,)).fetchall()
    conn.commit()
    return {
        "id": event[0],
        "name": event[1],
        "starts_at": event[2],
        "seats": [
            {"id": s[0], "label": s[1], "price_cents": s[2], "status": s[3]} for s in seats
        ],
    }


def sale_state(conn: psycopg.Connection, event_id: int) -> tuple[bool, bool] | None:
    """(sale_open, sold_out) by the database clock, or None if there's no such event."""
    row = conn.execute(
        "SELECT now() >= sale_opens_at, "
        "       NOT EXISTS (SELECT 1 FROM seats WHERE event_id = events.id AND status <> 'sold') "
        "FROM events WHERE id = %s",
        (event_id,),
    ).fetchone()
    conn.commit()
    return None if row is None else (row[0], row[1])
