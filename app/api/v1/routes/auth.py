"""Authentication endpoints."""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbSession
from app.core.errors import UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.repositories import user_repo
from app.schemas.auth import LoginRequest, TokenResponse, UserPublic
from app.schemas.common import ProblemDetail

log = structlog.get_logger(__name__)

# A genuine Argon2 hash of a random throwaway string, computed once at import.
# Verifying against it when the username does not exist makes the "no such user"
# path cost the same as the "wrong password" path, so response timing cannot be
# used to enumerate accounts. It must be a *real* hash - a hand-written constant
# would fail to parse, return instantly, and reintroduce the timing difference.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a JWT",
    responses={
        401: {"model": ProblemDetail, "description": "Invalid credentials"},
        429: {"model": ProblemDetail, "description": "Too many login attempts"},
    },
)
async def login(payload: LoginRequest, session: DbSession) -> TokenResponse:
    """Authenticate and return a bearer token.

    Seeded demo accounts (created by ``scripts/seed.py``):

    | username    | role        | can do                                  |
    |-------------|-------------|-----------------------------------------|
    | admin       | admin       | everything, including DELETE            |
    | contributor | contributor | create; update **only their own** records |
    | responder   | responder   | read-only                               |
    """
    user = await user_repo.get_by_username(session, payload.username)

    # One message and one code path for "no such user" and "wrong password".
    # Distinguishing them would turn this endpoint into a username oracle.
    # The password is verified even when the user is missing, so the response
    # time does not leak account existence either.
    password_hash = user.password_hash if user else _DUMMY_HASH
    password_ok = verify_password(payload.password, password_hash)

    if user is None or not password_ok:
        log.warning("login_failed", username=payload.username[:32])
        raise UnauthorizedError("Invalid username or password")

    if not user.is_active:
        log.warning("login_rejected_inactive", username=user.username)
        raise UnauthorizedError("This account has been deactivated")

    token, expires_in = create_access_token(
        subject=str(user.id), username=user.username, role=str(user.role)
    )

    log.info("login_succeeded", username=user.username, role=str(user.role))
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserPublic.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserPublic,
    summary="Describe the current token holder",
    responses={401: {"model": ProblemDetail}},
)
async def me(user: CurrentUser, session: DbSession) -> UserPublic:
    db_user = await user_repo.get_by_id(session, user.id)
    if db_user is None:
        raise UnauthorizedError("Token refers to a user that no longer exists")
    return UserPublic.model_validate(db_user)


@router.get(
    "/users",
    response_model=list[UserPublic],
    summary="List the seeded demo accounts",
    status_code=status.HTTP_200_OK,
)
async def list_users(user: CurrentUser, session: DbSession) -> list[UserPublic]:
    """Convenience endpoint for exploring the demo.

    Authentication is required, and only public fields are returned - no hashes.
    A real system would restrict this to admins; it is open to any authenticated
    caller here purely so a reviewer can see the seeded roles.
    """
    return [UserPublic.model_validate(u) for u in await user_repo.list_all(session)]

