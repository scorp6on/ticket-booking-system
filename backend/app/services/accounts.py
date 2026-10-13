import hashlib
import secrets

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SESSION_DAYS = 7
MIN_PASSWORD_LENGTH = 8

# Argon2 adds a random salt to every hash and is slow on purpose (~0.1s), so a stolen
# users table can't be cracked by trying billions of guesses.
hasher = PasswordHasher()


def _sha256(text: str) -> str:
    # Fine for session tokens and ID numbers: unlike passwords, the session token is
    # 32 random bytes, so there's nothing to guess. (ID numbers are a mock; see verify_id.)
    return hashlib.sha256(text.encode()).hexdigest()


def sign_up(conn: psycopg.Connection, email: str, password: str) -> int | None:
    """Create an account. Returns its id, or None if the email is already registered."""
    row = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING RETURNING id",
        (email.strip().lower(), hasher.hash(password)),
    ).fetchone()
    conn.commit()
    return None if row is None else row[0]


def log_in(conn: psycopg.Connection, email: str, password: str) -> str | None:
    """Check the password and start a session. Returns the session token for the cookie, or None."""
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = %s", (email.strip().lower(),)
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    user_id, password_hash = row
    try:
        hasher.verify(password_hash, password)
    except VerifyMismatchError:
        conn.commit()
        return None

    token = secrets.token_urlsafe(32)  # the browser keeps this; we only keep its hash
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) "
        "VALUES (%s, %s, now() + make_interval(days => %s))",
        (_sha256(token), user_id, SESSION_DAYS),
    )
    conn.commit()
    return token


def current_user(conn: psycopg.Connection, token: str) -> dict | None:
    """Who this session token belongs to, or None if it's unknown, logged out, or expired."""
    row = conn.execute(
        "SELECT u.id, u.email, u.id_verified_at IS NOT NULL FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > now()",
        (_sha256(token),),
    ).fetchone()
    conn.commit()
    return None if row is None else {"id": row[0], "email": row[1], "id_verified": row[2]}


def log_out(conn: psycopg.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_sha256(token),))
    conn.commit()


def verify_id(conn: psycopg.Connection, user_id: int, id_number: str) -> bool:
    """Mock ID check: any ID number passes, but each one can verify only ONE account.
    A real service would check the document is genuine and matches the person. Returns
    False if this ID already verified another account (one person, many accounts)."""
    try:
        conn.execute(
            # Verified once, for good: re-verifying can't swap in a different ID.
            "UPDATE users SET id_document_hash = %s, id_verified_at = now() "
            "WHERE id = %s AND id_verified_at IS NULL",
            (_sha256(id_number.strip().upper()), user_id),
        )
        conn.commit()
        return True
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return False
