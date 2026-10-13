import random
import secrets

import redis

# Mock CAPTCHA: a math question. It has the same shape as a real one (Cloudflare
# Turnstile, hCaptcha): the page solves a challenge and gets a proof, and the SERVER
# checks the proof. Swapping in a real one means replacing these two functions.
CHALLENGE_SECONDS = 120


def new_challenge(r: redis.Redis) -> dict:
    a, b = random.randint(1, 9), random.randint(1, 9)
    captcha_id = secrets.token_urlsafe(16)
    r.set(f"captcha:{captcha_id}", a + b, ex=CHALLENGE_SECONDS)
    return {"captcha_id": captcha_id, "question": f"What is {a} + {b}?"}


def passed(r: redis.Redis, captcha_id: str, answer: str) -> bool:
    # GETDEL: each challenge can be used once, so one solved puzzle can't let in many bots.
    expected = r.getdel(f"captcha:{captcha_id}")
    return expected is not None and answer.strip() == expected
