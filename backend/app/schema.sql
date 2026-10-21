CREATE TABLE IF NOT EXISTS users (
    id               BIGSERIAL PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE,
    password_hash    TEXT NOT NULL,               -- Argon2: salted and deliberately slow; never the password itself
    id_document_hash TEXT UNIQUE,                 -- hash of the verified ID number; UNIQUE = one account per real person
    id_verified_at   TIMESTAMPTZ,                 -- NULL until the ID check passes
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per login. Deleting the row logs that device out immediately.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,                 -- SHA-256 of the cookie's token, so a stolen table can't log anyone in
    user_id     BIGINT NOT NULL REFERENCES users(id),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    starts_at  TIMESTAMPTZ NOT NULL,
    sale_opens_at TIMESTAMPTZ NOT NULL DEFAULT now()  -- people who join the queue before this get a random place
);

-- Up to 3 seats one buyer holds and pays for together.
CREATE TABLE IF NOT EXISTS carts (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL,               -- the buyer; same value as seats.held_by
    event_id         BIGINT NOT NULL REFERENCES events(id),
    expires_at       TIMESTAMPTZ NOT NULL,        -- hold deadline, then payment deadline; each seat keeps a copy
    payment_attempts INT NOT NULL DEFAULT 0,      -- declines so far, for the whole cart
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seats (
    id               BIGSERIAL PRIMARY KEY,
    event_id         BIGINT NOT NULL REFERENCES events(id),
    label            TEXT NOT NULL,               -- e.g. '14B'
    price_cents      INT NOT NULL CHECK (price_cents > 0),  -- set by us, never by the browser
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'held', 'payment_pending', 'sold')),
    held_by          TEXT,                        -- user holding or owning the seat
    cart_id          BIGINT REFERENCES carts(id), -- the cart holding, paying for, or that bought the seat
    hold_expires_at  TIMESTAMPTZ,                 -- copy of the cart's deadline, so a claim checks one table
    UNIQUE (event_id, label)
);

-- One row per press of "Pay", covering every seat in the cart. Retries after a decline are new rows.
CREATE TABLE IF NOT EXISTS payments (
    id               BIGSERIAL PRIMARY KEY,       -- also sent to the bank, so we can ask about it even if no reply came
    cart_id          BIGINT NOT NULL REFERENCES carts(id),
    user_id          TEXT NOT NULL,               -- the payer; seats.held_by changes if a seat is lost
    amount_cents     INT NOT NULL CHECK (amount_cents > 0),
    idempotency_key  TEXT NOT NULL UNIQUE,        -- same key twice = same request, not a new charge
    bank_id          TEXT,                        -- bank's reference; NULL until the bank replies
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'declined', 'voided', 'captured')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "If a payment is processing, you can't start another": at most one pending
-- payment per cart, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_payment_per_cart
    ON payments (cart_id) WHERE status = 'pending';
