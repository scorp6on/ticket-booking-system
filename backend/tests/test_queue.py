import re
import threading

import pytest
import redis

from app.config import settings
from app.services import accounts, queue

EVENT = 1


def ranks(r, users):
    return [r.zrank(f"queue:{EVENT}", u) for u in users]


def in_room(r):
    return set(r.zrange(f"room:{EVENT}", 0, -1))


@pytest.fixture
def small_room(monkeypatch):
    monkeypatch.setattr(settings, "room_size", 3)


# --- Who goes first ---

def test_early_arrivals_are_shuffled(r):
    users = [f"user-{n}" for n in range(50)]
    for u in users:  # all arrive before the sale opens, in this order
        queue.join(r, EVENT, u, sale_open=False)
    # Arriving first gave no advantage. (Chance of the line matching arrival order: 1 in 50!)
    assert ranks(r, users) != list(range(50))


def test_late_arrivals_go_behind_early_ones_in_arrival_order(r):
    early = [f"early-{n}" for n in range(5)]
    late = [f"late-{n}" for n in range(3)]
    for u in early:
        queue.join(r, EVENT, u, sale_open=False)
    for u in late:
        queue.join(r, EVENT, u, sale_open=True)
    assert ranks(r, late) == [5, 6, 7]


def test_joining_again_keeps_your_spot(r):
    for u in ["alice", "bob", "carol"]:
        queue.join(r, EVENT, u, sale_open=True)
    queue.join(r, EVENT, "alice", sale_open=True)  # refresh, or a bot spamming join
    assert queue.status(r, EVENT, "alice") == {"state": "waiting", "position": 1}
    assert r.zcard(f"queue:{EVENT}") == 3


# --- Rolling admission ---

def test_room_fills_up_to_its_size(r, small_room):
    for u in ["a", "b", "c", "d", "e"]:
        queue.join(r, EVENT, u, sale_open=True)
    queue.admit(r, EVENT)
    assert in_room(r) == {"a", "b", "c"}
    assert queue.status(r, EVENT, "d") == {"state": "waiting", "position": 1}
    assert queue.status(r, EVENT, "a")["state"] == "admitted"


def test_buyer_leaving_lets_the_next_person_in(r, small_room):
    for u in ["a", "b", "c", "d"]:
        queue.join(r, EVENT, u, sale_open=True)
    queue.admit(r, EVENT)
    queue.leave_room(r, EVENT, "a")
    queue.admit(r, EVENT)
    assert in_room(r) == {"b", "c", "d"}


def test_expired_token_frees_the_spot(r, small_room):
    for u in ["a", "b", "c", "d"]:
        queue.join(r, EVENT, u, sale_open=True)
    queue.admit(r, EVENT)
    r.zadd(f"room:{EVENT}", {"a": 0})  # a's 5 minutes ran out
    queue.admit(r, EVENT)
    assert in_room(r) == {"b", "c", "d"}
    assert queue.status(r, EVENT, "a") == {"state": "not_in_line"}


# --- Leaving without saying so (closed tab) ---

def went_quiet(r, user, seconds=21):
    """Pretend this user's last check-in was `seconds` ago."""
    r.zadd(f"seen:{EVENT}", {user: (r.time()[0] - seconds) * 1000})


def test_quiet_person_in_line_loses_their_spot(r, small_room):
    for u in ["a", "b", "c", "d", "e"]:
        queue.join(r, EVENT, u, sale_open=True)
    went_quiet(r, "a")
    queue.admit(r, EVENT)
    assert in_room(r) == {"b", "c", "d"}
    assert queue.status(r, EVENT, "a") == {"state": "not_in_line"}


def test_quiet_person_in_the_room_frees_the_spot(r, small_room):
    for u in ["a", "b", "c", "d"]:
        queue.join(r, EVENT, u, sale_open=True)
    queue.admit(r, EVENT)
    went_quiet(r, "a")
    queue.admit(r, EVENT)
    assert in_room(r) == {"b", "c", "d"}


def test_19_seconds_of_quiet_is_fine(r, small_room):
    queue.join(r, EVENT, "a", sale_open=True)
    went_quiet(r, "a", seconds=19)
    queue.admit(r, EVENT)
    assert in_room(r) == {"a"}


def test_checking_in_keeps_your_spot(r, small_room):
    queue.join(r, EVENT, "a", sale_open=True)
    went_quiet(r, "a")
    queue.check_in(r, EVENT, "a")
    queue.admit(r, EVENT)
    assert in_room(r) == {"a"}


def test_checking_in_does_not_put_you_in_line(r):
    queue.check_in(r, EVENT, "stranger")
    assert r.zscore(f"seen:{EVENT}", "stranger") is None


def test_20_servers_admitting_at_once_never_overfill_the_room(r):
    for n in range(300):
        queue.join(r, EVENT, f"user-{n}", sale_open=True)
    servers = 20
    start_line = threading.Barrier(servers)

    def server():
        c = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        start_line.wait()
        queue.admit(c, EVENT)
        c.close()

    threads = [threading.Thread(target=server) for _ in range(servers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(in_room(r)) == settings.room_size == 100
    assert r.zcard(f"queue:{EVENT}") == 200


# --- Tokens ---

def valid_token(r, user="alice", event=EVENT, seconds=300):
    return queue.issue_token(event, user, (r.time()[0] + seconds) * 1000)


def test_token_works_for_its_owner(r):
    assert queue.token_is_valid(r, valid_token(r), EVENT, "alice") is True


def test_token_cannot_be_used_by_someone_else(r):
    assert queue.token_is_valid(r, valid_token(r), EVENT, "bot-1") is False


def test_token_cannot_be_used_for_another_event(r):
    assert queue.token_is_valid(r, valid_token(r), EVENT + 1, "alice") is False


def test_expired_token_is_rejected(r):
    assert queue.token_is_valid(r, valid_token(r, seconds=-1), EVENT, "alice") is False


def test_edited_token_is_rejected(r):
    # A bot takes Alice's token and swaps in its own name, keeping her signature.
    token = valid_token(r).replace(":alice:", ":bot-1:")
    assert queue.token_is_valid(r, token, EVENT, "bot-1") is False


def test_made_up_token_is_rejected(r):
    token = f"{EVENT}:99999999999999:bot-1:{'0' * 64}"
    assert queue.token_is_valid(r, token, EVENT, "bot-1") is False


# --- Through the API ---

def solved_captcha(client):
    challenge = client.get("/captcha").json()
    a, b = map(int, re.findall(r"\d+", challenge["question"]))
    return {"captcha_id": challenge["captcha_id"], "answer": str(a + b)}


def join_line(client, event_id, headers):
    return client.post(f"/events/{event_id}/queue", headers=headers, json=solved_captcha(client)).json()


def test_claim_without_a_queue_token_is_rejected(client, seat_id, buyer):
    resp = client.post("/carts", headers=buyer("bot")[1], json={"seat_ids": [seat_id]})
    assert resp.status_code == 422


def test_claim_with_a_forged_token_is_rejected(client, seat_id, event_id, buyer):
    account_id, cookie = buyer("bot")
    headers = {**cookie, "X-Queue-Token": f"{event_id}:99999999999999:{account_id}:{'0' * 64}"}
    resp = client.post("/carts", headers=headers, json={"seat_ids": [seat_id]})
    assert (resp.status_code, resp.json()) == (403, {"detail": "queue_token_invalid"})


def test_joining_needs_a_login(client, event_id):
    resp = client.post(f"/events/{event_id}/queue", json=solved_captcha(client))
    assert (resp.status_code, resp.json()) == (401, {"detail": "not_logged_in"})


def test_joining_needs_a_verified_id(client, conn, event_id):
    accounts.sign_up(conn, "new@example.com", "correct-horse")
    cookie = {"Cookie": f"session={accounts.log_in(conn, 'new@example.com', 'correct-horse')}"}
    resp = client.post(f"/events/{event_id}/queue", headers=cookie, json=solved_captcha(client))
    assert (resp.status_code, resp.json()) == (403, {"detail": "id_not_verified"})


def test_joining_needs_a_solved_captcha(client, event_id, buyer):
    cookie = buyer("alice")[1]
    no_captcha = client.post(f"/events/{event_id}/queue", headers=cookie)
    wrong = client.post(
        f"/events/{event_id}/queue", headers=cookie, json={**solved_captcha(client), "answer": "-1"}
    )
    assert (no_captcha.status_code, no_captcha.json()) == (403, {"detail": "captcha_required"})
    assert (wrong.status_code, wrong.json()) == (403, {"detail": "captcha_required"})


def test_one_solved_captcha_lets_in_only_one_buyer(client, event_id, buyer):
    proof = solved_captcha(client)
    first = client.post(f"/events/{event_id}/queue", headers=buyer("alice")[1], json=proof)
    second = client.post(f"/events/{event_id}/queue", headers=buyer("bot")[1], json=proof)
    assert first.status_code == 200
    assert (second.status_code, second.json()) == (403, {"detail": "captcha_required"})


def test_checking_your_place_needs_no_new_captcha(client, event_id, buyer):
    cookie = buyer("alice")[1]
    join_line(client, event_id, cookie)
    resp = client.post(f"/events/{event_id}/queue", headers=cookie)  # a later poll
    assert resp.status_code == 200


def test_waiting_room_before_the_sale_opens(client, conn, event_id, buyer):
    conn.execute("UPDATE events SET sale_opens_at = now() + interval '10 minutes'")
    conn.commit()
    status = join_line(client, event_id, buyer("alice")[1])
    assert status == {"state": "waiting", "position": 1, "sale_open": False}  # nobody let in early


def test_queue_to_seat_end_to_end(client, event_id, seat_id, buyer):
    cookie = buyer("alice")[1]
    status = join_line(client, event_id, cookie)
    assert status["state"] == "admitted"
    assert status["seconds_left"] == settings.queue_token_seconds

    resp = client.post("/carts", headers={**cookie, "X-Queue-Token": status["token"]}, json={"seat_ids": [seat_id]})
    assert resp.status_code == 201


def buy(client, headers, token, seat_id):
    cart_id = client.post("/carts", headers={**headers, "X-Queue-Token": token}, json={"seat_ids": [seat_id]}).json()["cart_id"]
    client.post(f"/carts/{cart_id}/pay", headers={**headers, "Idempotency-Key": "k1"}, json={"seat_ids": [seat_id]})


def test_line_is_told_when_sold_out(client, monkeypatch, event_id, seat_id, buyer):
    monkeypatch.setattr(settings, "room_size", 1)
    alice, bob = buyer("alice")[1], buyer("bob")[1]
    token = join_line(client, event_id, alice)["token"]
    assert join_line(client, event_id, bob) == {"state": "waiting", "position": 1, "sale_open": True}

    buy(client, alice, token, seat_id)

    # The only seat is sold, so Bob is told instead of waiting forever.
    assert join_line(client, event_id, bob) == {"state": "sold_out", "sale_open": True}


def test_purchase_frees_a_room_spot(client, conn, monkeypatch, event_id, seat_id, buyer):
    conn.execute("INSERT INTO seats (event_id, label, price_cents) VALUES (%s, '14C', 4999)", (event_id,))
    conn.commit()
    monkeypatch.setattr(settings, "room_size", 1)
    alice, bob = buyer("alice")[1], buyer("bob")[1]
    token = join_line(client, event_id, alice)["token"]
    join_line(client, event_id, bob)

    buy(client, alice, token, seat_id)

    assert client.post(f"/events/{event_id}/queue", headers=bob).json()["state"] == "admitted"


def test_seat_map_refresh_keeps_a_buyer_in_the_room(client, monkeypatch, event_id, buyer):
    monkeypatch.setattr(settings, "room_size", 1)
    alice_id, alice = buyer("alice")
    bob = buyer("bob")[1]
    join_line(client, event_id, alice)  # admitted
    join_line(client, event_id, bob)  # waiting
    r = client.app.state.redis

    def alice_went_quiet():
        r.zadd(f"seen:{event_id}", {str(alice_id): (r.time()[0] - 21) * 1000})

    alice_went_quiet()
    client.get(f"/events/{event_id}", headers=alice)  # alice is picking seats
    assert client.post(f"/events/{event_id}/queue", headers=bob).json()["state"] == "waiting"

    alice_went_quiet()  # alice closed the tab
    assert client.post(f"/events/{event_id}/queue", headers=bob).json()["state"] == "admitted"
