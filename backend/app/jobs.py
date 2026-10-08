import time

import psycopg

from app.config import settings
from app.services.bank import MockBank
from app.services.payments import void_stuck_payments

INTERVAL_SECONDS = 60  # worst case for a silent bank: payment window + this


def void_stuck_once(conn: psycopg.Connection, bank: MockBank) -> list[int]:
    """One pass: void stuck payments in our table, then release the money at the bank.
    Each stuck payment is returned to exactly one caller, even with several servers running this."""
    ids = void_stuck_payments(conn)
    for payment_id in ids:
        bank.void(payment_id)
    return ids


def main() -> None:
    bank = MockBank(replies=[])  # only void() is used here
    with psycopg.connect(settings.database_url) as conn:
        while True:
            ids = void_stuck_once(conn, bank)
            if ids:
                print(f"voided stuck payments: {ids}")
            time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
