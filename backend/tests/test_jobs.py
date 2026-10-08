import threading

import psycopg

from app.config import settings
from app.jobs import void_stuck_once
from app.services.bank import MockBank
from app.services.payments import create_payment


def make_stuck_payment(conn, seat_id, key):
    payment_id, _ = create_payment(conn, seat_id, f"user-{key}", 4999, key)
    conn.execute("UPDATE payments SET created_at = now() - interval '6 minutes' WHERE id = %s", (payment_id,))
    conn.commit()
    return payment_id


def test_job_releases_the_money_at_the_bank(seat_id, conn):
    stuck = make_stuck_payment(conn, seat_id, "k1")
    bank = MockBank(replies=[])

    assert void_stuck_once(conn, bank) == [stuck]
    assert bank.voided == [stuck]
    assert void_stuck_once(conn, bank) == []  # next run finds nothing
    assert bank.voided == [stuck]


def test_two_servers_running_the_job_never_void_twice(seat_id, conn):
    stuck = {make_stuck_payment(conn, seat_id, f"k{n}") for n in range(10)}
    bank = MockBank(replies=[])
    servers = 2
    start_line = threading.Barrier(servers)

    def server():
        with psycopg.connect(settings.database_url) as c:
            start_line.wait()
            void_stuck_once(c, bank)

    threads = [threading.Thread(target=server) for _ in range(servers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(bank.voided) == sorted(stuck)  # every payment voided exactly once
