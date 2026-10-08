CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    starts_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS seats (
    id               BIGSERIAL PRIMARY KEY,
    event_id         BIGINT NOT NULL REFERENCES events(id),
    label            TEXT NOT NULL,               -- e.g. '14B'
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'held', 'payment_pending', 'sold')),
    held_by          TEXT,                        -- user holding or owning the seat
    hold_expires_at  TIMESTAMPTZ,                 -- hold deadline, then payment deadline
    payment_attempts INT NOT NULL DEFAULT 0,      -- declines so far in this payment window
    UNIQUE (event_id, label)
);

-- One row per press of "Pay". Retries after a decline are new rows.
CREATE TABLE IF NOT EXISTS payments (
    id               BIGSERIAL PRIMARY KEY,       -- also sent to the bank, so we can ask about it even if no reply came
    seat_id          BIGINT NOT NULL REFERENCES seats(id),
    user_id          TEXT NOT NULL,               -- the payer; seats.held_by changes if the seat is lost
    amount_cents     INT NOT NULL CHECK (amount_cents > 0),
    idempotency_key  TEXT NOT NULL UNIQUE,        -- same key twice = same request, not a new charge
    bank_id          TEXT,                        -- bank's reference; NULL until the bank replies
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'declined', 'voided', 'captured')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "If a payment is processing, you can't start another": at most one pending
-- payment per buyer per seat, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_payment_per_buyer
    ON payments (seat_id, user_id) WHERE status = 'pending';
