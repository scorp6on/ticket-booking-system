import psycopg
import redis
from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import settings
from app.db import get_conn, get_redis
from app.services import checkout, events, queue, seats
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


# Join the line if not in it yet, then report where you stand. The page calls this
# every few seconds; each call also lets people in as spots free up.
@router.post("/events/{event_id}/queue")
def queue_status(
    event_id: int,
    user_id: str = Header(alias="X-User-Id"),
    conn: psycopg.Connection = Depends(get_conn),
    r: redis.Redis = Depends(get_redis),
):
    state = events.sale_state(conn, event_id)
    if state is None:
        raise HTTPException(404, "event_not_found")
    sale_open, sold_out = state
    if sold_out:
        return {"state": "sold_out", "sale_open": sale_open}
    queue.join(r, event_id, user_id, sale_open)
    if sale_open:
        queue.admit(r, event_id)
    return {**queue.status(r, event_id, user_id), "sale_open": sale_open}


# X-User-Id stands in for a real login until the anti-bot milestone adds accounts.
# X-Queue-Token proves this buyer waited their turn; calling the API directly doesn't skip the line.
@router.post("/seats/{seat_id}/claim")
def claim(
    seat_id: int,
    user_id: str = Header(alias="X-User-Id"),
    queue_token: str = Header(alias="X-Queue-Token"),
    conn: psycopg.Connection = Depends(get_conn),
    r: redis.Redis = Depends(get_redis),
):
    event_id = seats.seat_event_id(conn, seat_id)
    if event_id is None:
        raise HTTPException(404, "seat_not_found")
    if not queue.token_is_valid(r, queue_token, event_id, user_id):
        raise HTTPException(403, "queue_token_invalid")
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
    r: redis.Redis = Depends(get_redis),
):
    price = seats.seat_price(conn, seat_id)
    if price is None:
        raise HTTPException(404, "seat_not_found")
    result = checkout.pay(conn, bank, seat_id, user_id, price, idempotency_key)
    if result.outcome == checkout.SOLD:
        queue.leave_room(r, seats.seat_event_id(conn, seat_id), user_id)  # rolling: next person gets in
    # seconds_left restarts the page's countdown: paying opens a new window. None = no window left.
    return {
        "outcome": result.outcome,
        "attempts_left": result.attempts_left,
        "seconds_left": seats.seconds_left(conn, seat_id, user_id),
    }
