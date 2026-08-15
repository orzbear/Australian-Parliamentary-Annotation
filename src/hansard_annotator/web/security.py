"""Password, session-token, and CSRF primitives."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

SESSION_COOKIE = "hansard_session"
LOGIN_CSRF_COOKIE = "hansard_login_csrf"


def password_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=3,
        memory_cost=65_536,
        parallelism=2,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


_PASSWORD_HASHER = password_hasher()
_DUMMY_HASH = _PASSWORD_HASHER.hash("not-a-real-password-for-timing")


def hash_password(password: str, minimum_length: int) -> str:
    if len(password) < minimum_length:
        raise ValueError(f"password must contain at least {minimum_length} characters")
    if len(password) > 1024:
        raise ValueError("password is too long")
    return _PASSWORD_HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    candidate = stored_hash or _DUMMY_HASH
    try:
        valid = _PASSWORD_HASHER.verify(candidate, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(valid and stored_hash is not None)


def random_token() -> str:
    return urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def token_digest(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def csrf_token(session_token: str, csrf_hash: str, secret: str) -> str:
    material = f"{session_token}:{csrf_hash}"
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()


def verify_csrf(
    supplied: str | None,
    session_token: str,
    csrf_hash: str,
    secret: str,
) -> bool:
    if not supplied:
        return False
    expected = csrf_token(session_token, csrf_hash, secret)
    return hmac.compare_digest(supplied, expected)


def make_login_csrf(secret: str) -> str:
    payload = f"{int(time.time())}:{random_token()}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    encoded = urlsafe_b64encode(payload.encode() + b"." + signature)
    return encoded.decode("ascii")


def verify_login_csrf(token: str | None, secret: str, max_age: int = 900) -> bool:
    if not token:
        return False
    try:
        raw = urlsafe_b64decode(token.encode())
        if len(raw) < 34 or raw[-33:-32] != b".":
            return False
        payload, signature = raw[:-33], raw[-32:]
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        timestamp = int(payload.split(b":", 1)[0])
    except (ValueError, TypeError):
        return False
    return (
        hmac.compare_digest(signature, expected)
        and 0 <= int(time.time()) - timestamp <= max_age
    )
