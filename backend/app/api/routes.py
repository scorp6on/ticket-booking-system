import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import settings
from app.db import get_conn
from app.services import checkout, events, seats
from app.services.bank import MockBank

router = APIRouter()

bank = MockBank(replies=[])  # always approves; tests swap in a scripted bank


def get_bank() -> MockBank:
    return bank


@router.get("/events/{event_id}")
def get_event(event_id: int, conn: psycopg.Connection = Depends(get_conn)):
    event = events.get_event(conn, event_id)
    if event is None:
        raise HTTPException(404, "event_not_found")
    return event


# X-User-Id stands in for a real login until the anti-bot milestone adds accounts.
@router.post("/seats/{seat_id}/claim")
def claim(
    seat_id: int,
    user_id: str = Header(alias="X-User-Id"),
    conn: psycopg.Connection = Depends(get_conn),
):
    if not seats.claim_seat(conn, seat_id, user_id):
        raise HTTPException(409, "seat_taken")
    return {"seconds_left": settings.hold_seconds}


# Used for the first try and every retry; a retry just sends a new Idempotency-Key.
# No amount in the request: the price comes from our database.
@router.post("/seats/{seat_id}/pay")
def pay(
    seat_id: int,
    user_id: str = Header(alias="X-User-Id"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    conn: psycopg.Connection = Depends(get_conn),
    bank: MockBank = Depends(get_bank),
):
    price = seats.seat_price(conn, seat_id)
    if price is None:
        raise HTTPException(404, "seat_not_found")
    result = checkout.pay(conn, bank, seat_id, user_id, price, idempotency_key)
    # seconds_left restarts the page's countdown: paying opens a new window. None = no window left.
    return {
        "outcome": result.outcome,
        "attempts_left": result.attempts_left,
        "seconds_left": seats.seconds_left(conn, seat_id, user_id),
    }
