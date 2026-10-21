from app.services import queue


def seat_status(client, event_id):
    return client.get(f"/events/{event_id}").json()["seats"][0]["status"]


# --- GET /events/{id} ---

def test_event_lists_its_seats(client, event_id, seat_id):
    body = client.get(f"/events/{event_id}").json()
    assert body["name"] == "Test Show"
    assert body["seats"] == [{"id": seat_id, "label": "14B", "price_cents": 4999, "status": "open"}]


def test_expired_hold_shows_as_open(client, conn, event_id, seat_id):
    conn.execute(
        "UPDATE seats SET status = 'held', held_by = 'alice', "
        "hold_expires_at = now() - interval '1 second' WHERE id = %s",
        (seat_id,),
    )
    conn.commit()
    assert seat_status(client, event_id) == "open"


def test_unknown_event_is_404(client):
    assert client.get("/events/999").status_code == 404


# --- POST /carts ---

def hold(client, headers, seat_ids):
    return client.post("/carts", headers=headers, json={"seat_ids": seat_ids})


def pay(client, headers, cart_id, seat_ids):
    return client.post(f"/carts/{cart_id}/pay", headers=headers, json={"seat_ids": seat_ids})


def test_hold_then_someone_else_gets_409(client, event_id, seat_id, admitted):
    first = hold(client, admitted("alice"), [seat_id])
    second = hold(client, admitted("bob"), [seat_id])

    assert first.status_code == 201
    assert first.json() == {"cart_id": first.json()["cart_id"], "held": [seat_id], "taken": [], "seconds_left": 300}
    assert (second.status_code, second.json()) == (409, {"detail": "seat_taken"})
    assert seat_status(client, event_id) == "held"


def test_hold_lists_the_seats_someone_else_got(client, seats, admitted):
    hold(client, admitted("bob"), [seats[1]])
    resp = hold(client, admitted("alice"), seats[:3])

    assert (resp.json()["held"], resp.json()["taken"]) == ([seats[0], seats[2]], [seats[1]])


def test_hold_without_logging_in_is_rejected(client, seat_id, event_id, r):
    token = queue.issue_token(event_id, "1", (r.time()[0] + 300) * 1000)
    resp = hold(client, {"X-Queue-Token": token}, [seat_id])
    assert (resp.status_code, resp.json()) == (401, {"detail": "not_logged_in"})


def test_hold_unknown_seat_is_404(client, admitted):
    assert hold(client, admitted("alice"), [999]).status_code == 404


def test_second_cart_is_rejected(client, seats, admitted):
    hold(client, admitted("alice"), [seats[0]])
    resp = hold(client, admitted("alice"), [seats[1]])
    assert (resp.status_code, resp.json()) == (409, {"detail": "cart_active"})


# --- POST /carts/{id}/seats ---

def test_add_a_replacement_seat(client, seats, admitted):
    hold(client, admitted("bob"), [seats[1]])
    cart_id = hold(client, admitted("alice"), seats[:2]).json()["cart_id"]

    resp = client.post(f"/carts/{cart_id}/seats", headers=admitted("alice"), json={"seat_id": seats[2]})

    assert resp.status_code == 200
    assert resp.json()["seconds_left"] <= 300


def test_add_to_a_cart_being_paid_for_is_rejected(client, bank, seats, admitted, paying):
    bank.replies = ["decline"]
    cart_id = hold(client, admitted("alice"), seats[:1]).json()["cart_id"]
    pay(client, paying("alice", "k1"), cart_id, seats[:1])

    resp = client.post(f"/carts/{cart_id}/seats", headers=admitted("alice"), json={"seat_id": seats[1]})
    assert (resp.status_code, resp.json()) == (409, {"detail": "cart_locked"})


# --- POST /carts/{id}/pay ---

def test_hold_and_pay_sells_the_seat(client, event_id, seat_id, admitted, paying):
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]
    r = pay(client, paying("alice", "k1"), cart_id, [seat_id])

    assert r.json() == {"outcome": "sold", "attempts_left": None, "seconds_left": None, "expired_seat_ids": []}
    assert seat_status(client, event_id) == "sold"


def test_retry_after_decline_uses_the_same_endpoint(client, bank, seat_id, admitted, paying):
    bank.replies = ["decline", "approve"]
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]

    first = pay(client, paying("alice", "k1"), cart_id, [seat_id])
    retry = pay(client, paying("alice", "k2"), cart_id, [seat_id])

    assert first.json()["outcome"] == "declined"
    assert first.json()["attempts_left"] == 2
    assert retry.json()["outcome"] == "sold"


def test_paying_restarts_the_countdown(client, conn, bank, seat_id, admitted, paying):
    bank.replies = ["decline"]
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]
    # Alice used up 4 of her 5 hold minutes before pressing Pay.
    conn.execute("UPDATE carts SET expires_at = now() + interval '1 minute' WHERE id = %s", (cart_id,))
    conn.execute("UPDATE seats SET hold_expires_at = now() + interval '1 minute' WHERE id = %s", (seat_id,))
    conn.commit()

    r = pay(client, paying("alice", "k1"), cart_id, [seat_id])

    assert r.json()["outcome"] == "declined"
    assert 295 <= r.json()["seconds_left"] <= 300  # a fresh 5-minute payment window


def test_double_click_asks_the_bank_once(client, bank, seat_id, admitted, paying):
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]
    pay(client, paying("alice", "k1"), cart_id, [seat_id])
    again = pay(client, paying("alice", "k1"), cart_id, [seat_id])

    assert again.json()["outcome"] == "sold"
    assert len(bank.authorized) == 1


def test_price_comes_from_the_server_not_the_request(client, conn, seat_id, admitted, paying):
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]
    # A bot tries to set its own price.
    client.post(
        f"/carts/{cart_id}/pay", headers=paying("alice", "k1"), json={"seat_ids": [seat_id], "amount_cents": 1}
    )

    assert conn.execute("SELECT amount_cents FROM payments").fetchone()[0] == 4999


def test_screen_out_of_date_is_told_the_cart_changed(client, bank, seats, admitted, paying):
    cart_id = hold(client, admitted("alice"), seats[:2]).json()["cart_id"]
    r = pay(client, paying("alice", "k1"), cart_id, seats[:1])

    assert r.json()["outcome"] == "cart_changed"
    assert bank.authorized == []


def test_pay_without_idempotency_key_is_rejected(client, bank, seat_id, admitted, paying):
    cart_id = hold(client, admitted("alice"), [seat_id]).json()["cart_id"]
    assert pay(client, paying("alice"), cart_id, [seat_id]).status_code == 422
    assert bank.authorized == []


def test_pay_for_unknown_cart_is_404(client, paying):
    assert pay(client, paying("alice", "k1"), 999, [1]).status_code == 404
