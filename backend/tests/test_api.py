def as_user(user_id, key=None):
    headers = {"X-User-Id": user_id}
    if key:
        headers["Idempotency-Key"] = key
    return headers


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


# --- POST /seats/{id}/claim ---

def test_claim_then_someone_else_gets_409(client, event_id, seat_id, admitted):
    first = client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    second = client.post(f"/seats/{seat_id}/claim", headers=admitted("bob"))

    assert (first.status_code, first.json()) == (200, {"seconds_left": 300})
    assert (second.status_code, second.json()) == (409, {"detail": "seat_taken"})
    assert seat_status(client, event_id) == "held"


def test_claim_without_a_user_is_rejected(client, seat_id):
    assert client.post(f"/seats/{seat_id}/claim").status_code == 422


# --- POST /seats/{id}/pay ---

def test_claim_and_pay_sells_the_seat(client, event_id, seat_id, admitted):
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    r = client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"))

    assert r.json() == {"outcome": "sold", "attempts_left": None, "seconds_left": None}
    assert seat_status(client, event_id) == "sold"


def test_retry_after_decline_uses_the_same_endpoint(client, bank, seat_id, admitted):
    bank.replies = ["decline", "approve"]
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))

    first = client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"))
    retry = client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k2"))

    assert first.json()["outcome"] == "declined"
    assert first.json()["attempts_left"] == 2
    assert retry.json()["outcome"] == "sold"


def test_paying_restarts_the_countdown(client, conn, bank, seat_id, admitted):
    bank.replies = ["decline"]
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    # Alice used up 4 of her 5 hold minutes before pressing Pay.
    conn.execute("UPDATE seats SET hold_expires_at = now() + interval '1 minute' WHERE id = %s", (seat_id,))
    conn.commit()

    r = client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"))

    assert r.json()["outcome"] == "declined"
    assert 295 <= r.json()["seconds_left"] <= 300  # a fresh 5-minute payment window


def test_double_click_asks_the_bank_once(client, bank, seat_id, admitted):
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"))
    again = client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"))

    assert again.json()["outcome"] == "sold"
    assert len(bank.authorized) == 1


def test_price_comes_from_the_server_not_the_request(client, conn, seat_id, admitted):
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    # A bot tries to set its own price.
    client.post(f"/seats/{seat_id}/pay", headers=as_user("alice", "k1"), json={"amount_cents": 1})

    assert conn.execute("SELECT amount_cents FROM payments").fetchone()[0] == 4999


def test_pay_without_idempotency_key_is_rejected(client, bank, seat_id, admitted):
    client.post(f"/seats/{seat_id}/claim", headers=admitted("alice"))
    assert client.post(f"/seats/{seat_id}/pay", headers=as_user("alice")).status_code == 422
    assert bank.authorized == []


def test_pay_for_unknown_seat_is_404(client):
    assert client.post("/seats/999/pay", headers=as_user("alice", "k1")).status_code == 404
