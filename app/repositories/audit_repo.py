"""Audit log writes. Append-only by construction: there is no update or delete."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.enums import AuditAction


async def record(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: AuditAction,
    actor_id: uuid.UUID | None = None,
    actor_username: str | None = None,
    changes: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    """Append one entry."""
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        actor_username=actor_username,
        changes=changes,
        request_id=request_id,
    )
    session.add(entry)
    return entry


async def list_for_entity(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Build a {field: {from, to}} map of what actually changed."""
    changes: dict[str, Any] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if old_value != new_value:
            changes[key] = {"from": _jsonable(old_value), "to": _jsonable(new_value)}
    return changes


def _jsonable(value: Any) -> Any:
    """Coerce to something JSONB can store."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)
