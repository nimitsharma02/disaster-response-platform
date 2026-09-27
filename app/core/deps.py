"""FastAPI dependencies: database session, current user, role gates,."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, UnauthorizedError
from app.db.models.enums import UserRole
from app.db.session import get_db_session
from app.repositories import user_repo
from app.schemas.auth import AuthenticatedUser
from app.schemas.common import PaginationParams

log = structlog.get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="JWT from POST /auth/login")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_db_session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedUser:
    """Resolve the caller from the Authorization: Bearer header."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing bearer token. Obtain one from POST /api/v1/auth/login.")

    payload = decode_or_raise(credentials.credentials)

    try:
        user_id = payload["sub"]
    except KeyError as exc:
        raise UnauthorizedError("Token is missing the subject claim") from exc

    import uuid as _uuid

    try:
        user_uuid = _uuid.UUID(str(user_id))
    except ValueError as exc:
        raise UnauthorizedError("Token subject is not a valid identifier") from exc

    user = await user_repo.get_by_id(session, user_uuid)
    if user is None:
        raise UnauthorizedError("Token refers to a user that no longer exists")
    if not user.is_active:
        raise ForbiddenError("This account has been deactivated")

    authenticated = AuthenticatedUser(id=user.id, username=user.username, role=user.role)
    request.state.user = authenticated
    return authenticated


def decode_or_raise(token: str) -> dict:
    from app.core.security import decode_access_token

    return decode_access_token(token)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_roles(*allowed: UserRole):
    """Build a dependency that admits only the listed roles."""

    async def _guard(user: CurrentUser) -> AuthenticatedUser:
        if user.role not in allowed:
            allowed_names = ", ".join(sorted(str(r) for r in allowed))
            log.warning(
                "authz_denied_role",
                actor=user.username,
                actor_role=str(user.role),
                required=allowed_names,
            )
            raise ForbiddenError(
                f"This action requires one of: {allowed_names}. "
                f"Your role is '{user.role}'."
            )
        return user

    return _guard


RequireAdmin = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]
RequireWriter = Annotated[
    AuthenticatedUser, Depends(require_roles(UserRole.ADMIN, UserRole.CONTRIBUTOR))
]
RequireAnyRole = Annotated[
    AuthenticatedUser,
    Depends(require_roles(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.RESPONDER)),
]


async def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100, description="Rows per page (max 100)")] = 20,
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
) -> PaginationParams:
    return PaginationParams(limit=limit, offset=offset)


Pagination = Annotated[PaginationParams, Depends(get_pagination)]


def get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


RequestId = Annotated[str | None, Depends(get_request_id)]
