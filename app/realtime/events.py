"""Real-time event bus."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from app.cache.backend import get_backend

log = structlog.get_logger(__name__)

CHANNEL = "drp:events"


class EventType(StrEnum):
    DISASTER_CREATED = "disaster_created"
    DISASTER_UPDATED = "disaster_updated"
    DISASTER_DELETED = "disaster_deleted"
    RESOURCE_CREATED = "resource_created"
    REPORTS_REFRESHED = "reports_refreshed"


def build_envelope(event_type: EventType | str, data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload in a consistent envelope."""
    return {
        "id": str(uuid.uuid4()),
        "event": str(event_type),
        "ts": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def publish(event_type: EventType | str, data: dict[str, Any]) -> dict[str, Any]:
    """Publish an event."""
    envelope = build_envelope(event_type, data)
    try:
        await get_backend().publish(CHANNEL, json.dumps(envelope, default=str))
        log.info(
            "event_published",
            event_type=envelope["event"],
            event_id=envelope["id"],
        )
    except Exception as exc:  # noqa: BLE001
        log.error("event_publish_failed", event_type=str(event_type), error=str(exc))
    return envelope


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Yield decoded events until the consumer stops iterating."""
    backend = get_backend()
    async for raw in backend.subscribe(CHANNEL):
        try:
            yield json.loads(raw)
        except ValueError:
            log.warning("event_decode_failed", raw=str(raw)[:120])


def disaster_payload(disaster: Any, *, distance_km: float | None = None) -> dict[str, Any]:
    """Compact event payload for a disaster."""
    from app.repositories.geo import point_to_lat_lng

    coords = point_to_lat_lng(getattr(disaster, "location", None))
    return {
        "id": str(disaster.id),
        "title": disaster.title,
        "status": str(disaster.status),
        "tags": list(disaster.tags or []),
        "location_name": disaster.location_name,
        "latitude": coords[0] if coords else None,
        "longitude": coords[1] if coords else None,
        "created_by": str(disaster.created_by),
        "updated_at": disaster.updated_at.isoformat() if disaster.updated_at else None,
        "distance_km": distance_km,
    }
