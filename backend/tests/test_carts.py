import threading
import time

import psycopg

from app.config import settings
from app.services.carts import (
    add_seat,
    confirm_payment,
    hold_seats,
    record_declined_payment,
    seconds_left,
    start_payment,
)


def seat_rows(conn, seat_ids):
    """(status, held_by, cart_id) per seat, in the order given."""
    rows = dict(
        (r[0], r[1:])
        for r in conn.execute("SELECT id, status, held_by, cart_id FROM seats WHERE id = ANY(%s)", (seat_ids,))
    )
    return [rows[s] for s in seat_ids]


def statuses(conn, seat_ids):
    return [row[0] for row in seat_rows(conn, seat_ids)]


def deadline_copies_match(conn, cart_id):
    """Every seat still in the cart has exactly the cart's deadline."""
    mismatched = conn.execute(
        "SELECT count(*) FROM seats s JOIN carts c ON c.id = s.cart_id "
        "WHERE c.id = %s AND s.status IN ('held', 'payment_pending') AND s.hold_expires_at <> c.expires_at",
        (cart_id,),
    ).fetchone()[0]
    return mismatched == 0


def expire(conn, cart_id):
    """The cart's time ran out a second ago (both copies of the deadline)."""
    conn.execute("UPDATE carts SET expires_at = now() - interval '1 second' WHERE id = %s", (cart_id,))
    conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE cart_id = %s", (cart_id,))
    conn.commit()


def expire_one_seat(conn, seat_id):
    """Only this seat's copy ran out. Can't happen if the copies stay in sync, but it's
    the case the all-or-nothing rule exists for."""
    conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", (seat_id,))
    conn.commit()


def holding(conn, event_id, account_id, seat_ids):
    result = hold_seats(conn, account_id, event_id, seat_ids)
    assert result.outcome == "held"
    return result.cart_id


# --- Holding seats ---

def test_100_buyers_with_overlapping_carts_never_share_a_seat(conn, event_id, seats, account):
    buyers = 100
    ids = [account(f"user-{n}") for n in range(buyers)]
    start_line = threading.Barrier(buyers)  # everyone waits here, then all go at once
    results, errors = {}, []

    def buyer(n):
        # Each buyer wants 3 seats next to each other, overlapping with other buyers'.
        # Each gets its own connection, like separate web requests would.
        wanted = seats[n % 4 : n % 4 + 3]
        try:
            with psycopg.connect(settings.database_url) as c:
                start_line.wait()
                results[n] = hold_seats(c, ids[n], event_id, wanted)
        except Exception as e:  # a deadlock would show up here
            errors.append(e)

    threads = [threading.Thread(target=buyer, args=(n,)) for n in range(buyers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    won = [seat for r in results.values() for seat in r.held]
    assert len(won) == len(set(won))  # no seat won twice
    assert sorted(won) == seats  # every seat went to someone
    for n, r in results.items():
        for seat in r.held:
            assert seat_rows(conn, [seat]) == [("held", str(ids[n]), r.cart_id)]


def test_hold_reports_taken_seats_and_keeps_the_rest(conn, event_id, seats, account):
    holding(conn, event_id, account("bob"), [seats[1]])

    result = hold_seats(conn, account("alice"), event_id, seats[:3])

    assert (result.outcome, result.held, result.taken) == ("held", [seats[0], seats[2]], [seats[1]])
    assert statuses(conn, seats[:3]) == ["held", "held", "held"]
    assert deadline_copies_match(conn, result.cart_id)


def test_all_seats_taken_creates_no_cart(conn, event_id, seats, account):
    holding(conn, event_id, account("bob"), seats[:2])

    result = hold_seats(conn, account("alice"), event_id, seats[:2])

    assert (result.outcome, result.cart_id, result.taken) == ("taken", None, seats[:2])
    assert conn.execute("SELECT count(*) FROM carts").fetchone()[0] == 1  # only Bob's


def test_one_cart_at_a_time(conn, event_id, seats, account):
    holding(conn, event_id, account("alice"), [seats[0]])
    assert hold_seats(conn, account("alice"), event_id, [seats[1]]).outcome == "cart_active"


def test_new_cart_allowed_once_the_old_one_expired(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), [seats[0]])
    expire(conn, cart)
    assert hold_seats(conn, account("alice"), event_id, [seats[1]]).outcome == "held"


def test_seats_in_an_expired_cart_can_be_claimed(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), seats[:2])
    expire(conn, cart)

    result = hold_seats(conn, account("bob"), event_id, seats[:2])

    assert result.held == seats[:2]
    assert seat_rows(conn, seats[:2]) == [("held", str(account("bob")), result.cart_id)] * 2


def test_sold_seat_cannot_be_claimed(conn, event_id, seats, account):
    conn.execute("UPDATE seats SET status = 'sold', held_by = 'alice' WHERE id = %s", (seats[0],))
    conn.commit()
    assert hold_seats(conn, account("bob"), event_id, [seats[0]]).outcome == "taken"


# --- Replacing a taken seat ---

def test_replacement_ends_at_the_carts_deadline(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    # A minute is left on the cart.
    conn.execute("UPDATE carts SET expires_at = now() + interval '1 minute' WHERE id = %s", (cart,))
    conn.execute("UPDATE seats SET hold_expires_at = now() + interval '1 minute' WHERE cart_id = %s", (cart,))
    conn.commit()

    assert add_seat(conn, alice, cart, seats[2]) == "held"
    assert deadline_copies_match(conn, cart)
    assert seconds_left(conn, alice, cart) <= 60  # not a fresh 5 minutes


def test_replacement_that_is_taken(conn, event_id, seats, account):
    holding(conn, event_id, account("bob"), [seats[2]])
    cart = holding(conn, event_id, account("alice"), seats[:2])
    assert add_seat(conn, account("alice"), cart, seats[2]) == "taken"


def test_cannot_add_to_an_expired_cart(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), seats[:2])
    expire(conn, cart)
    assert add_seat(conn, account("alice"), cart, seats[2]) == "cart_expired"


def test_cannot_add_to_someone_elses_cart(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), seats[:2])
    assert add_seat(conn, account("bob"), cart, seats[2]) == "not_found"


def test_cannot_add_past_the_ticket_limit(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), seats[:3])
    assert add_seat(conn, account("alice"), cart, seats[3]) == "limit_reached"


def test_cart_is_locked_once_payment_starts(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    start_payment(conn, alice, cart, seats[:2])
    assert add_seat(conn, alice, cart, seats[2]) == "cart_locked"


def test_adding_a_seat_while_pay_starts_never_splits_the_cart(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    start_line = threading.Barrier(2)
    results = {}

    def add():
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results["add"] = add_seat(c, alice, cart, seats[2])

    def pay():
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results["pay"] = start_payment(c, alice, cart, seats[:2]).outcome

    threads = [threading.Thread(target=add), threading.Thread(target=pay)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Whichever went first, the cart never has some seats paying and others just held.
    assert results in (
        {"add": "held", "pay": "cart_changed"},  # the add won: the screen showed 2 seats, the cart has 3
        {"add": "cart_locked", "pay": "started"},  # Pay won: the cart was locked
    )
    in_cart = conn.execute(
        "SELECT DISTINCT status FROM seats WHERE cart_id = %s", (cart,)
    ).fetchall()
    assert len(in_cart) == 1


# --- Starting payment: every seat on screen, or none ---

def test_start_payment_moves_every_seat(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:3])

    assert start_payment(conn, alice, cart, seats[:3]).outcome == "started"
    assert statuses(conn, seats[:3]) == ["payment_pending"] * 3
    assert deadline_copies_match(conn, cart)


def test_paying_restarts_the_countdown(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    conn.execute("UPDATE carts SET expires_at = now() + interval '1 minute' WHERE id = %s", (cart,))
    conn.execute("UPDATE seats SET hold_expires_at = now() + interval '1 minute' WHERE cart_id = %s", (cart,))
    conn.commit()

    start_payment(conn, alice, cart, seats[:2])

    assert 295 <= seconds_left(conn, alice, cart) <= 300
    assert deadline_copies_match(conn, cart)


def test_no_seat_moves_if_one_was_lost(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:3])
    expire_one_seat(conn, seats[1])
    holding(conn, event_id, account("bob"), [seats[1]])  # Bob takes it; it leaves Alice's cart

    result = start_payment(conn, alice, cart, seats[:3])

    assert (result.outcome, result.expired) == ("expired", [seats[1]])
    assert statuses(conn, [seats[0], seats[2]]) == ["held", "held"]  # rolled back, not paying


def test_pay_for_the_seats_you_have_after_losing_one(conn, event_id, seats, account):
    alice = account("alice")
    holding(conn, event_id, account("bob"), [seats[1]])
    cart = hold_seats(conn, alice, event_id, seats[:3]).cart_id  # gets seats 0 and 2

    assert start_payment(conn, alice, cart, [seats[0], seats[2]]).outcome == "started"


def test_screen_showing_fewer_seats_than_the_cart_is_rejected(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])

    assert start_payment(conn, alice, cart, [seats[0]]).outcome == "cart_changed"
    assert statuses(conn, seats[:2]) == ["held", "held"]


def test_someone_elses_seat_in_the_list_is_rejected(conn, event_id, seats, account):
    alice = account("alice")
    holding(conn, event_id, account("bob"), [seats[2]])
    cart = holding(conn, event_id, alice, seats[:2])

    result = start_payment(conn, alice, cart, seats[:3])

    assert (result.outcome, result.expired) == ("expired", [seats[2]])
    assert statuses(conn, seats[:2]) == ["held", "held"]


def test_someone_else_cannot_start_payment(conn, event_id, seats, account):
    cart = holding(conn, event_id, account("alice"), seats[:2])
    assert start_payment(conn, account("bob"), cart, seats[:2]).outcome == "not_holder"


def test_expired_cart_cannot_start_payment(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    expire(conn, cart)
    assert start_payment(conn, alice, cart, seats[:2]).outcome == "not_holder"


def test_paying_seats_cannot_be_claimed(conn, event_id, seats, account):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:2])
    start_payment(conn, alice, cart, seats[:2])
    assert hold_seats(conn, account("bob"), event_id, seats[:2]).outcome == "taken"


# --- Bank answers ---

def alice_paying(conn, event_id, seats, account, n=2):
    alice = account("alice")
    cart = holding(conn, event_id, alice, seats[:n])
    start_payment(conn, alice, cart, seats[:n])
    return alice, cart


def test_approval_sells_every_seat(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    assert confirm_payment(conn, alice, cart, seats[:2]) is True
    assert seat_rows(conn, seats[:2]) == [("sold", str(alice), cart)] * 2


def test_approval_sells_nothing_if_a_seat_is_missing(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    assert confirm_payment(conn, alice, cart, seats[:3]) is False  # seat 3 was never paid for
    assert statuses(conn, seats[:2]) == ["payment_pending"] * 2


def test_late_approval_is_rejected_even_if_nobody_claimed_the_seats(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    expire(conn, cart)
    assert confirm_payment(conn, alice, cart, seats[:2]) is False  # caller must void the charge
    assert "sold" not in statuses(conn, seats[:2])


def test_expired_payment_window_puts_seats_back_on_sale(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    expire(conn, cart)
    assert hold_seats(conn, account("bob"), event_id, seats[:2]).held == seats[:2]


def test_declines_count_for_the_whole_cart(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    assert record_declined_payment(conn, alice, cart) == 2
    assert statuses(conn, seats[:2]) == ["payment_pending"] * 2


def test_third_decline_releases_every_seat(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    assert [record_declined_payment(conn, alice, cart) for _ in range(3)] == [2, 1, 0]
    assert seat_rows(conn, seats[:2]) == [("open", None, None)] * 2
    assert hold_seats(conn, account("bob"), event_id, seats[:2]).held == seats[:2]


def test_new_cart_gets_fresh_attempts(conn, event_id, seats, account):
    alice, cart = alice_paying(conn, event_id, seats, account)
    for _ in range(3):
        record_declined_payment(conn, alice, cart)
    bob = account("bob")
    bob_cart = holding(conn, event_id, bob, seats[:2])
    start_payment(conn, bob, bob_cart, seats[:2])
    assert record_declined_payment(conn, bob, bob_cart) == 2


def test_start_payment_locks_seats_in_id_order(conn, event_id, seats, account):
    # Claims lock seats in id order. If starting payment locked a later seat while
    # waiting for an earlier one, a claim holding the earlier seat and wanting the
    # later one would wait on it forever: a deadlock, and Postgres kills one of them.
    alice = account("alice")
    first, second = seats[:2]
    cart = holding(conn, event_id, alice, [first, second])
    # Move the first seat's row so it's stored after the second one, as real updates do
    # over time. Changing an indexed column forces the move. A scan in storage order
    # now meets the second seat first.
    conn.execute("UPDATE seats SET label = label || '-moved' WHERE id = %s", (first,))
    conn.execute("UPDATE seats SET label = replace(label, '-moved', '') WHERE id = %s", (first,))
    conn.commit()

    with psycopg.connect(settings.database_url) as other_buyer, psycopg.connect(settings.database_url) as probe:
        other_buyer.execute("SELECT 1 FROM seats WHERE id = %s FOR UPDATE", (first,))  # mid-claim on the first seat
        results = []

        def pay():
            with psycopg.connect(settings.database_url) as c:
                results.append(start_payment(c, alice, cart, [first, second]).outcome)

        t = threading.Thread(target=pay)
        t.start()
        time.sleep(0.5)  # start_payment is now waiting for the first seat
        try:
            # The second seat must still be free to lock: start_payment hasn't taken it out of order.
            probe.execute("SELECT 1 FROM seats WHERE id = %s FOR UPDATE NOWAIT", (second,))
            locked_out_of_order = False
        except psycopg.errors.LockNotAvailable:
            locked_out_of_order = True
        probe.rollback()
        other_buyer.rollback()
        t.join()

    assert locked_out_of_order is False
    assert results == ["started"]
