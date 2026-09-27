"""Password hashing and JWT issuing/verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings
from app.core.errors import UnauthorizedError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification that never raises on a bad password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def create_access_token(
    *,
    subject: str,
    username: str,
    role: str,
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    minutes = expires_minutes if expires_minutes is not None else settings.jwt_expires_minutes
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=minutes)

    payload: dict[str, Any] = {
        "sub": subject,                # standard: the user id
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),   # issued at
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid.uuid4()),      # unique id, the hook a future denylist would use
        "iss": "disaster-response-platform",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(minutes * 60)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a token, or raise :class:`UnauthorizedError`."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="disaster-response-platform",
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid authentication token") from exc
