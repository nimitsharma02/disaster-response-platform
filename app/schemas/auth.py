"""Authentication schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"username": "admin", "password": "admin123"}}
    )

    username: str = Field(min_length=3, max_length=64)
    # No max_length that would truncate a passphrase, and no complexity rules:
    # Argon2 handles any input, and arbitrary composition rules mostly push users
    # toward weaker, more predictable passwords.
    password: str = Field(min_length=6, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")
    user: UserPublic


class UserPublic(ORMModel):
    """The only user representation that ever leaves the service.

    ``password_hash`` is absent by construction rather than stripped on the way
    out - a field that does not exist on the response model cannot be leaked by
    a future careless change.
    """

    id: uuid.UUID
    username: str
    display_name: str | None = None
    role: UserRole
    is_active: bool
    created_at: datetime


class AuthenticatedUser(BaseModel):
    """Request-scoped identity, assembled from the JWT plus a database read."""

    id: uuid.UUID
    username: str
    role: UserRole

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


# Resolve the forward reference used in TokenResponse.
TokenResponse.model_rebuild()
