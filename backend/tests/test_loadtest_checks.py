import pytest

from app.loadtest import BOT_CHECKS, CHECKS, check
from app.seed import create_event
from app.services import accounts

# The load test's proof is only as good as its checks. Each test here fakes one way a
# buyer could be cheated and makes sure the matching check catches it.


@pytest.fixture
def event(conn):
    event_id = create_event(conn, "Load Test", 0)
    seats = [r[0] for r in conn.execute("SELECT id FROM seats WHERE event_id = %s ORDER BY id", (event_id,))]
    return event_id, seats


def sell(conn, seat_id, buyer, payments=1, amount=None, status="captured"):
    conn.execute("UPDATE seats SET status = 'sold', held_by = %s WHERE id = %s", (buyer, seat_id))
    for n in range(payments):
        conn.execute(
            "INSERT INTO payments (seat_id, user_id, amount_cents, idempotency_key, status) "
            "SELECT id, %s, COALESCE(%s, price_cents), %s, %s FROM seats WHERE id = %s",
            (buyer, amount, f"{seat_id}-{buyer}-{n}", status, seat_id),
        )
    conn.commit()


def failing(conn, event_id):
    return [rule for rule, sql in CHECKS.items() if conn.execute(sql, {"event": event_id}).fetchall()]


def failing_bot_checks(conn, event_id):
    return [
        rule for rule, (sql, bots) in BOT_CHECKS.items()
        if conn.execute(sql, {"event": event_id, "bots": bots}).fetchall()
    ]


def test_honest_sales_pass(conn, event):
    event_id, seats = event
    for n, seat in enumerate(seats):
        sell(conn, seat, str(n + 1))
    assert failing(conn, event_id) == []
    assert check(event_id) is True


def test_seat_paid_for_twice_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], "1", payments=2)
    assert failing(conn, event_id) == [
        "Every sold seat has exactly one captured payment (no double-sale, no free seat)"
    ]


def test_seat_given_away_free_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], "1", payments=0)
    assert failing(conn, event_id) == [
        "Every sold seat has exactly one captured payment (no double-sale, no free seat)"
    ]


def test_charge_without_a_seat_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], "1")
    conn.execute("UPDATE seats SET status = 'open', held_by = NULL WHERE id = %s", (seats[0],))
    conn.commit()
    assert "No charge without a seat" in failing(conn, event_id)


def test_wrong_person_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], "1")
    conn.execute("UPDATE seats SET held_by = '2' WHERE id = %s", (seats[0],))  # Bob paid, Alice got it
    conn.commit()
    assert failing(conn, event_id) == ["The person who paid is the person who got the seat"]


def test_wrong_amount_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], "1", amount=1)
    assert failing(conn, event_id) == ["Charged the seat's price"]


def test_fourth_seat_is_caught(conn, event):
    event_id, seats = event
    for s in seats[:4]:
        sell(conn, s, "1")
    assert failing(conn, event_id) == ["No buyer has more than 3 seats"]


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_stuck_payment_is_caught(conn, event, status):
    event_id, seats = event
    sell(conn, seats[0], "1")
    sell(conn, seats[1], "2", status=status)
    assert "No stuck payments (pending or approved-but-not-captured)" in failing(conn, event_id)


# --- Bots (after attack.js) ---

def bot(conn, kind):
    return str(accounts.sign_up(conn, f"bot-{kind}-1@example.com", "correct-horse"))


def sell_the_rest(conn, seats):
    for n, seat in enumerate(seats):
        sell(conn, seat, f"fan-{n}")


def test_bot_merely_holding_a_seat_is_caught(conn, event):
    event_id, seats = event
    sell_the_rest(conn, seats[1:])
    conn.execute(
        "UPDATE seats SET status = 'held', held_by = %s, hold_expires_at = now() + interval '5 minutes' WHERE id = %s",
        (bot(conn, "skipper"), seats[0]),
    )
    conn.commit()
    assert "Line-skipping bots got no seats" in failing_bot_checks(conn, event_id)


def test_hammerer_buying_a_seat_is_caught(conn, event):
    event_id, seats = event
    sell(conn, seats[0], bot(conn, "hammerer"))
    sell_the_rest(conn, seats[1:])
    assert failing_bot_checks(conn, event_id) == ["Queue-hammering bots (arriving after the fans) got no seats"]


def test_scalper_with_3_seats_passes_and_4_is_caught(conn, event):
    event_id, seats = event
    scalper = bot(conn, "scalper")
    for seat in seats[:3]:
        sell(conn, seat, scalper)
    sell_the_rest(conn, seats[4:])
    sell(conn, seats[3], "fan-x")
    assert failing_bot_checks(conn, event_id) == []

    conn.execute("UPDATE payments SET user_id = %s WHERE seat_id = %s", (scalper, seats[3]))
    conn.execute("UPDATE seats SET held_by = %s WHERE id = %s", (scalper, seats[3]))
    conn.commit()
    assert failing_bot_checks(conn, event_id) == ["The scalper's 20 tabs got at most 3 seats"]


def test_unsold_seat_is_caught(conn, event):
    event_id, seats = event
    sell_the_rest(conn, seats[1:])
    assert failing_bot_checks(conn, event_id) == ["Every seat sold (bots didn't leave seats stuck)"]
