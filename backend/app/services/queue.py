import hashlib
import hmac
import random

import redis

from app.config import settings

# Two Redis sorted sets per event (members are user ids, kept ordered by score):
#   queue:{event}  the line. Score = place in line; lowest goes first.
#   room:{event}   people choosing seats. Score = when their token expires (ms).


def _keys(event_id: int) -> tuple[str, str]:
    return f"queue:{event_id}", f"room:{event_id}"


def _now_ms(r: redis.Redis) -> int:
    # Redis's clock, so every app server agrees on the time (same idea as Postgres now()).
    seconds, micros = r.time()
    return seconds * 1000 + micros // 1000


def join(r: redis.Redis, event_id: int, user_id: str, sale_open: bool) -> None:
    """Get in line. Joining again keeps your current spot."""
    queue_key, room_key = _keys(event_id)
    if r.zscore(room_key, user_id) is not None:
        return  # already choosing seats
    # Before the sale: a random score below 1 shuffles everyone who came early, so being
    # a fast bot doesn't help. After: arrival time (a huge number) puts latecomers behind
    # them, first come, first served.
    score = random.random() if not sale_open else _now_ms(r)
    r.zadd(queue_key, {user_id: score}, nx=True)  # nx: one spot per user


# Runs inside Redis as one indivisible step: two callers can never both see
# "3 spots free" and admit 6 people. Expired tokens leave the room here, so
# no background job is needed.
ADMIT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local free = tonumber(ARGV[1]) - redis.call('ZCARD', KEYS[2])
if free > 0 then
  local admitted = redis.call('ZPOPMIN', KEYS[1], free)
  for i = 1, #admitted, 2 do
    redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), admitted[i])
  end
end
"""


def admit(r: redis.Redis, event_id: int) -> None:
    """Rolling admission: fill the room back up to room_size from the front of the line."""
    r.eval(ADMIT_LUA, 2, *_keys(event_id), settings.room_size, settings.queue_token_seconds * 1000)


def leave_room(r: redis.Redis, event_id: int, user_id: str) -> None:
    """Free the spot early (they bought a seat) so the next person gets in."""
    r.zrem(_keys(event_id)[1], user_id)


def status(r: redis.Redis, event_id: int, user_id: str) -> dict:
    """Where this user stands: in the room (with a token) or waiting (with a position)."""
    queue_key, room_key = _keys(event_id)
    expires_ms = r.zscore(room_key, user_id)
    if expires_ms is not None:
        expires_ms = int(expires_ms)
        return {
            "state": "admitted",
            "token": issue_token(event_id, user_id, expires_ms),
            "seconds_left": max(0, -(-(expires_ms - _now_ms(r)) // 1000)),  # round up, like seats.seconds_left
        }
    rank = r.zrank(queue_key, user_id)
    if rank is None:
        return {"state": "not_in_line"}
    return {"state": "waiting", "position": rank + 1}


# A token is "event:expires:user:signature". The signature is made with a secret only
# the server knows, so a bot can't invent a token or change the user or expiry in one.
def _sign(payload: str) -> str:
    return hmac.new(settings.queue_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(event_id: int, user_id: str, expires_ms: int) -> str:
    payload = f"{event_id}:{expires_ms}:{user_id}"
    return f"{payload}:{_sign(payload)}"


def token_is_valid(r: redis.Redis, token: str, event_id: int, user_id: str) -> bool:
    payload, _, signature = token.rpartition(":")
    if not hmac.compare_digest(signature, _sign(payload)):  # compare_digest: no timing hints for forgers
        return False
    token_event, expires_ms, token_user = payload.split(":", 2)
    return token_event == str(event_id) and token_user == user_id and int(expires_ms) > _now_ms(r)
