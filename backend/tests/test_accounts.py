import threading

import psycopg
import pytest

from app.config import settings
from app.services import accounts
from app.services.seats import claim_seat_within_limit

CREDS = {"email": "alice@example.com", "password": "correct-horse"}


def sign_up_and_log_in(client, creds=CREDS):
    client.post("/accounts", json=creds)
    return client.post("/sessions", json=creds)


# --- Sign up and log in ---

def test_sign_up_log_in_and_see_who_you_are(client):
    assert client.post("/accounts", json=CREDS).status_code == 201
    assert client.post("/sessions", json=CREDS).status_code == 200
    assert client.get("/me").json() == {"email": "alice@example.com", "id_verified": False}


def test_email_can_only_be_registered_once(client):
    client.post("/accounts", json=CREDS)
    again = client.post("/accounts", json={**CREDS, "email": "ALICE@example.com"})
    assert (again.status_code, again.json()) == (409, {"detail": "email_taken"})


def test_short_password_is_rejected(client):
    assert client.post("/accounts", json={**CREDS, "password": "short"}).status_code == 422


def test_wrong_password_and_unknown_email_get_the_same_answer(client):
    client.post("/accounts", json=CREDS)
    wrong_password = client.post("/sessions", json={**CREDS, "password": "wrong-horse"})
    unknown_email = client.post("/sessions", json={**CREDS, "email": "nobody@example.com"})
    # Same answer for both, so a guesser can't learn which emails have accounts.
    assert wrong_password.json() == unknown_email.json() == {"detail": "wrong_email_or_password"}
    assert wrong_password.status_code == unknown_email.status_code == 401


def test_me_needs_a_login(client):
    assert client.get("/me").status_code == 401


# --- What a stolen database would reveal ---

def test_passwords_are_stored_as_salted_argon2_hashes(client, conn):
    client.post("/accounts", json=CREDS)
    client.post("/accounts", json={**CREDS, "email": "bob@example.com"})  # same password
    alice_hash, bob_hash = [row[0] for row in conn.execute("SELECT password_hash FROM users ORDER BY id")]

    assert "correct-horse" not in alice_hash
    assert alice_hash.startswith("$argon2")
    assert alice_hash != bob_hash  # the salt: same password, different hashes


def test_session_tokens_are_stored_hashed(client, conn):
    sign_up_and_log_in(client)
    token = client.cookies["session"]
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != token


def test_session_cookie_is_hidden_from_page_scripts(client):
    resp = sign_up_and_log_in(client)
    assert "httponly" in resp.headers["set-cookie"].lower()


# --- Sessions ending ---

def test_log_out_kills_the_session_immediately(client):
    sign_up_and_log_in(client)
    token = client.cookies["session"]
    client.delete("/sessions")
    # Even someone who copied the token can't use it anymore.
    assert client.get("/me", headers={"Cookie": f"session={token}"}).status_code == 401


def test_expired_session_is_rejected(client, conn):
    sign_up_and_log_in(client)
    conn.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    conn.commit()
    assert client.get("/me").status_code == 401


# --- ID verification ---

def test_verifying_an_id(client):
    sign_up_and_log_in(client)
    assert client.post("/me/verify-id", json={"id_number": "D1234567"}).json() == {"id_verified": True}
    assert client.get("/me").json()["id_verified"] is True


def test_one_id_can_verify_only_one_account(client, conn):
    # A scalper makes a second account and reuses the same ID.
    first = accounts.sign_up(conn, "alice@example.com", "correct-horse")
    second = accounts.sign_up(conn, "alice2@example.com", "correct-horse")
    assert accounts.verify_id(conn, first, "D1234567") is True
    assert accounts.verify_id(conn, second, "d1234567 ") is False  # spacing and case don't help


def test_reused_id_is_409_through_the_api(client, conn):
    other = accounts.sign_up(conn, "other@example.com", "correct-horse")
    accounts.verify_id(conn, other, "D1234567")
    sign_up_and_log_in(client)
    resp = client.post("/me/verify-id", json={"id_number": "D1234567"})
    assert (resp.status_code, resp.json()) == (409, {"detail": "id_already_used"})
    assert client.get("/me").json()["id_verified"] is False


# --- 3 tickets per person ---

@pytest.fixture
def six_seats(conn, event_id):
    for n in range(6):
        conn.execute(
            "INSERT INTO seats (event_id, label, price_cents) VALUES (%s, %s, 4999)", (event_id, f"R{n}")
        )
    conn.commit()
    return [r[0] for r in conn.execute("SELECT id FROM seats WHERE label LIKE 'R%%' ORDER BY id")]


@pytest.fixture
def alice_id(conn):
    return accounts.sign_up(conn, "alice@example.com", "correct-horse")


def test_fourth_seat_is_refused(conn, six_seats, alice_id):
    results = [claim_seat_within_limit(conn, s, alice_id) for s in six_seats[:4]]
    assert results == ["held", "held", "held", "limit_reached"]


def test_held_and_sold_seats_both_count(conn, six_seats, alice_id):
    claim_seat_within_limit(conn, six_seats[0], alice_id)
    claim_seat_within_limit(conn, six_seats[1], alice_id)
    conn.execute("UPDATE seats SET status = 'sold', hold_expires_at = NULL WHERE id = %s", (six_seats[0],))
    conn.commit()
    assert claim_seat_within_limit(conn, six_seats[2], alice_id) == "held"
    assert claim_seat_within_limit(conn, six_seats[3], alice_id) == "limit_reached"


def test_expired_holds_dont_count(conn, six_seats, alice_id):
    for s in six_seats[:3]:
        claim_seat_within_limit(conn, s, alice_id)
    conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", (six_seats[0],))
    conn.commit()
    assert claim_seat_within_limit(conn, six_seats[3], alice_id) == "held"


def test_four_tabs_at_once_cannot_beat_the_limit(conn, six_seats, alice_id):
    # Alice already holds 2 seats, then clicks 4 more in 4 tabs at the same instant.
    claim_seat_within_limit(conn, six_seats[0], alice_id)
    claim_seat_within_limit(conn, six_seats[1], alice_id)
    tabs = 4
    start_line = threading.Barrier(tabs)
    results = []

    def tab(seat_id):
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results.append(claim_seat_within_limit(c, seat_id, alice_id))

    threads = [threading.Thread(target=tab, args=(s,)) for s in six_seats[2:]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["held", "limit_reached", "limit_reached", "limit_reached"]
    held = conn.execute("SELECT count(*) FROM seats WHERE held_by = %s", (str(alice_id),)).fetchone()[0]
    assert held == 3


def test_limit_through_the_api(client, event_id, six_seats, admitted):
    for s in six_seats[:3]:
        assert client.post(f"/seats/{s}/claim", headers=admitted("alice")).status_code == 200
    resp = client.post(f"/seats/{six_seats[3]}/claim", headers=admitted("alice"))
    assert (resp.status_code, resp.json()) == (409, {"detail": "ticket_limit_reached"})
