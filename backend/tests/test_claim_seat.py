import threading

import psycopg

from app.config import settings
from app.services.seats import (
    claim_seat,
    confirm_payment,
    record_declined_payment,
    start_payment,
)


def seat_row(conn, seat_id):
    return conn.execute(
        "SELECT status, held_by FROM seats WHERE id = %s", (seat_id,)
    ).fetchone()


def test_100_simultaneous_buyers_exactly_one_wins(seat_id, conn):
    buyers = 100
    start_line = threading.Barrier(buyers)  # everyone waits here, then all go at once
    results = {}

    def buyer(n):
        # Each buyer gets its own connection, like separate web requests would.
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            results[f"user-{n}"] = claim_seat(c, seat_id, f"user-{n}")

    threads = [threading.Thread(target=buyer, args=(n,)) for n in range(buyers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [user for user, won in results.items() if won]
    assert len(results) == buyers
    assert len(winners) == 1
    assert seat_row(conn, seat_id) == ("held", winners[0])


def test_active_hold_blocks_others(seat_id, conn):
    assert claim_seat(conn, seat_id, "alice") is True
    assert claim_seat(conn, seat_id, "bob") is False
    assert seat_row(conn, seat_id) == ("held", "alice")


def test_expired_hold_can_be_claimed(seat_id, conn):
    # Alice's hold ran out a second ago; no cleanup job has run.
    conn.execute(
        "UPDATE seats SET status = 'held', held_by = 'alice', "
        "hold_expires_at = now() - interval '1 second' WHERE id = %s",
        (seat_id,),
    )
    conn.commit()
    assert claim_seat(conn, seat_id, "bob") is True
    assert seat_row(conn, seat_id) == ("held", "bob")


def test_sold_seat_cannot_be_claimed(seat_id, conn):
    conn.execute("UPDATE seats SET status = 'sold', held_by = 'alice' WHERE id = %s", (seat_id,))
    conn.commit()
    assert claim_seat(conn, seat_id, "bob") is False


def test_holder_can_start_payment(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    assert start_payment(conn, seat_id, "alice") is True
    assert seat_row(conn, seat_id) == ("payment_pending", "alice")


def test_non_holder_cannot_start_payment(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    assert start_payment(conn, seat_id, "bob") is False
    assert seat_row(conn, seat_id) == ("held", "alice")


def test_expired_hold_cannot_start_payment(seat_id, conn):
    # Carol's hold ran out 30 seconds ago and nobody has claimed the seat since.
    conn.execute(
        "UPDATE seats SET status = 'held', held_by = 'carol', "
        "hold_expires_at = now() - interval '30 seconds' WHERE id = %s",
        (seat_id,),
    )
    conn.commit()
    assert start_payment(conn, seat_id, "carol") is False


def test_payment_pending_seat_cannot_be_claimed(seat_id, conn):
    claim_seat(conn, seat_id, "alice")
    start_payment(conn, seat_id, "alice")
    # Even if the original 5-minute hold would have ended, the payment deadline is what counts now.
    assert claim_seat(conn, seat_id, "bob") is False
    assert seat_row(conn, seat_id) == ("payment_pending", "alice")


def expire_deadline(conn, seat_id):
    conn.execute(
        "UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s",
        (seat_id,),
    )
    conn.commit()


def alice_paying(conn, seat_id):
    claim_seat(conn, seat_id, "alice")
    start_payment(conn, seat_id, "alice")


def test_approved_payment_sells_seat(seat_id, conn):
    alice_paying(conn, seat_id)
    assert confirm_payment(conn, seat_id, "alice") is True
    assert seat_row(conn, seat_id) == ("sold", "alice")


def test_approval_for_someone_else_is_rejected(seat_id, conn):
    alice_paying(conn, seat_id)
    assert confirm_payment(conn, seat_id, "bob") is False
    assert seat_row(conn, seat_id) == ("payment_pending", "alice")


def test_late_approval_is_rejected_even_if_nobody_claimed_the_seat(seat_id, conn):
    alice_paying(conn, seat_id)
    expire_deadline(conn, seat_id)
    assert confirm_payment(conn, seat_id, "alice") is False  # caller must void the charge
    assert seat_row(conn, seat_id)[0] != "sold"


def test_expired_payment_window_puts_seat_back_on_sale(seat_id, conn):
    alice_paying(conn, seat_id)
    expire_deadline(conn, seat_id)
    assert claim_seat(conn, seat_id, "bob") is True
    assert seat_row(conn, seat_id) == ("held", "bob")


def test_decline_keeps_seat_for_retry(seat_id, conn):
    alice_paying(conn, seat_id)
    assert record_declined_payment(conn, seat_id, "alice") == 2
    assert seat_row(conn, seat_id) == ("payment_pending", "alice")


def test_third_decline_releases_seat(seat_id, conn):
    alice_paying(conn, seat_id)
    assert record_declined_payment(conn, seat_id, "alice") == 2
    assert record_declined_payment(conn, seat_id, "alice") == 1
    assert record_declined_payment(conn, seat_id, "alice") == 0
    assert seat_row(conn, seat_id) == ("open", None)
    assert claim_seat(conn, seat_id, "bob") is True


def test_new_buyer_gets_fresh_attempts(seat_id, conn):
    alice_paying(conn, seat_id)
    for _ in range(3):
        record_declined_payment(conn, seat_id, "alice")
    claim_seat(conn, seat_id, "bob")
    start_payment(conn, seat_id, "bob")
    assert record_declined_payment(conn, seat_id, "bob") == 2
