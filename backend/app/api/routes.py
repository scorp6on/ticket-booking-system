import redis
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_pool, get_redis
from app.services import accounts, captcha, checkout, events, queue, seats
from app.services.bank import MockBank

router = APIRouter()

bank = MockBank(replies=[])  # always approves; tests swap in a scripted bank

# Every endpoint takes a database connection and gives it back inside its own body
# (`with pool.connection()`), never across steps. See the note in main.py.


def get_bank() -> MockBank:
    return bank


# --- Accounts ---

class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=accounts.MIN_PASSWORD_LENGTH, max_length=200)


class IdDocument(BaseModel):
    id_number: str = Field(min_length=4, max_length=64)


def optional_account(
    session: str | None = Cookie(default=None),
    pool: ConnectionPool = Depends(get_pool),
) -> dict | None:
    """The logged-in account from the session cookie, or None."""
    if not session:
        return None
    with pool.connection() as conn:
        return accounts.current_user(conn, session)


def current_account(account: dict | None = Depends(optional_account)) -> dict:
    """The logged-in account. Every buying action requires one."""
    if account is None:
        raise HTTPException(401, "not_logged_in")
    return account


@router.post("/accounts", status_code=201)
def sign_up(body: Credentials, pool: ConnectionPool = Depends(get_pool)):
    with pool.connection() as conn:
        if accounts.sign_up(conn, body.email, body.password) is None:
            raise HTTPException(409, "email_taken")
    return {"ok": True}


@router.post("/sessions")
def log_in(body: Credentials, response: Response, pool: ConnectionPool = Depends(get_pool)):
    with pool.connection() as conn:
        token = accounts.log_in(conn, body.email, body.password)
    if token is None:
        raise HTTPException(401, "wrong_email_or_password")  # don't say which one: no hints for guessers
    # HttpOnly: page scripts can't read it, so injected JavaScript can't steal it.
    # SameSite=lax: other sites can't make the browser send it with their requests.
    # (In production, also secure=True so it only travels over HTTPS.)
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=accounts.SESSION_DAYS * 86400
    )
    return {"ok": True}


@router.delete("/sessions")
def log_out(
    response: Response,
    session: str | None = Cookie(default=None),
    pool: ConnectionPool = Depends(get_pool),
):
    if session:
        with pool.connection() as conn:
            accounts.log_out(conn, session)  # the token stops working everywhere, right now
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/me")
def me(account: dict = Depends(current_account)):
    return {"email": account["email"], "id_verified": account["id_verified"]}


@router.post("/me/verify-id")
def verify_id(
    body: IdDocument,
    account: dict = Depends(current_account),
    pool: ConnectionPool = Depends(get_pool),
):
    with pool.connection() as conn:
        if not accounts.verify_id(conn, account["id"], body.id_number):
            raise HTTPException(409, "id_already_used")  # one real person, one account
    return {"id_verified": True}


# --- Events and the queue ---

# The seat map refreshes every few seconds. For a logged-in buyer, that also counts as
# "still here", so people choosing seats keep their spot in the room.
@router.get("/events/{event_id}")
def get_event(
    event_id: int,
    account: dict | None = Depends(optional_account),
    pool: ConnectionPool = Depends(get_pool),
    r: redis.Redis = Depends(get_redis),
):
    with pool.connection() as conn:
        event = events.get_event(conn, event_id)
    if event is None:
        raise HTTPException(404, "event_not_found")
    if account is not None:
        queue.check_in(r, event_id, str(account["id"]))
    return event


@router.get("/captcha")
def new_captcha(r: redis.Redis = Depends(get_redis)):
    return captcha.new_challenge(r)


class CaptchaAnswer(BaseModel):
    captcha_id: str
    answer: str


# Join the line if not in it yet, then report where you stand. The page calls this
# every few seconds; each call also lets people in as spots free up.
# Layers, cheapest first: logged in, ID verified, CAPTCHA solved (only to join).
@router.post("/events/{event_id}/queue")
def queue_status(
    event_id: int,
    body: CaptchaAnswer | None = None,
    account: dict = Depends(current_account),
    pool: ConnectionPool = Depends(get_pool),
    r: redis.Redis = Depends(get_redis),
):
    if not account["id_verified"]:
        raise HTTPException(403, "id_not_verified")
    with pool.connection() as conn:
        state = events.sale_state(conn, event_id)
    if state is None:
        raise HTTPException(404, "event_not_found")
    sale_open, sold_out = state
    if sold_out:
        return {"state": "sold_out", "sale_open": sale_open}

    user_id = str(account["id"])
    queue.check_in(r, event_id, user_id)  # before admitting, so this caller isn't dropped as gone
    if queue.status(r, event_id, user_id)["state"] == "not_in_line":
        if body is None or not captcha.passed(r, body.captcha_id, body.answer):
            raise HTTPException(403, "captcha_required")
        queue.join(r, event_id, user_id, sale_open)
    if sale_open:
        queue.admit(r, event_id)
    return {**queue.status(r, event_id, user_id), "sale_open": sale_open}


# --- Buying ---

# X-Queue-Token proves this buyer waited their turn; calling the API directly doesn't skip the line.
@router.post("/seats/{seat_id}/claim")
def claim(
    seat_id: int,
    queue_token: str = Header(alias="X-Queue-Token"),
    account: dict = Depends(current_account),
    pool: ConnectionPool = Depends(get_pool),
    r: redis.Redis = Depends(get_redis),
):
    user_id = str(account["id"])
    with pool.connection() as conn:
        event_id = seats.seat_event_id(conn, seat_id)
        if event_id is None:
            raise HTTPException(404, "seat_not_found")
        queue.check_in(r, event_id, user_id)
        if not queue.token_is_valid(r, queue_token, event_id, user_id):
            raise HTTPException(403, "queue_token_invalid")
        result = seats.claim_seat_within_limit(conn, seat_id, account["id"])
    if result == "limit_reached":
        raise HTTPException(409, "ticket_limit_reached")
    if result == "taken":
        raise HTTPException(409, "seat_taken")
    return {"seconds_left": settings.hold_seconds}


# Used for the first try and every retry; a retry just sends a new Idempotency-Key.
# No amount in the request: the price comes from our database.
@router.post("/seats/{seat_id}/pay")
def pay(
    seat_id: int,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    account: dict = Depends(current_account),
    pool: ConnectionPool = Depends(get_pool),
    bank: MockBank = Depends(get_bank),
    r: redis.Redis = Depends(get_redis),
):
    user_id = str(account["id"])
    with pool.connection() as conn:
        price = seats.seat_price(conn, seat_id)
        if price is None:
            raise HTTPException(404, "seat_not_found")
        event_id = seats.seat_event_id(conn, seat_id)
        queue.check_in(r, event_id, user_id)
        result = checkout.pay(conn, bank, seat_id, user_id, price, idempotency_key)
        if result.outcome == checkout.SOLD:
            queue.leave_room(r, event_id, user_id)  # rolling: next person gets in
        # seconds_left restarts the page's countdown: paying opens a new window. None = no window left.
        seconds_left = seats.seconds_left(conn, seat_id, user_id)
    return {"outcome": result.outcome, "attempts_left": result.attempts_left, "seconds_left": seconds_left}
