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
