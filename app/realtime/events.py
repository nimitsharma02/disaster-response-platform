"""Real-time event bus.

Events are published to a Redis pub/sub channel and fanned out from there to
every connected WebSocket and SSE client:

    service         Redis channel        transport         clients
    -------         -------------        ---------         -------
    publish() ----> drp:events -----> WebSocket /events/ws -> browser map
                               \\----> SSE /events/stream ---> curl -N

**Why via Redis rather than a process-local list of connections.** With more than
one uvicorn worker, a connection lives in exactly one process. An in-process
broadcast would reach only the clients that happened to land on the worker
handling the write - everyone else silently misses the event. Pub/sub means any
worker can publish and every worker delivers. The ``MemoryBackend`` fallback
keeps this working single-process when Redis is absent, with that caveat logged.

**Why publish after commit.** Publishing before the transaction commits would
announce a change that can still be rolled back, and clients that immediately
GET the resource would get a 404. Events are emitted by the service layer only
once the write is durable.

**Delivery is best-effort, at-most-once.** There is no replay buffer and no
acknowledgement. A client that disconnects misses what happened while it was
away and must re-fetch on reconnect. That is the correct trade for live
situational awareness - the REST API remains the source of truth, this is a
notification that it changed.
"""

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
    """Wrap a payload in a consistent envelope.

    ``id`` lets a client de-duplicate if it briefly holds two connections
    (which happens during a reconnect), and ``ts`` lets it discard events that
    arrived out of order.
    """
    return {
        "id": str(uuid.uuid4()),
        "event": str(event_type),
        "ts": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def publish(event_type: EventType | str, data: dict[str, Any]) -> dict[str, Any]:
    """Publish an event. Never raises.

    A failure to broadcast must not fail the write that triggered it: the data
    is already committed, and returning a 500 would tell the client their
    perfectly successful update did not happen.
    """
    envelope = build_envelope(event_type, data)
    try:
        await get_backend().publish(CHANNEL, json.dumps(envelope, default=str))
        log.info(
            "event_published",
            # NOT `event=`: structlog reserves that for the event name itself.
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
    """Compact event payload for a disaster.

    Deliberately a summary, not the full record. Events are broadcast to every
    connected client, so they should carry enough to update a map marker and no
    more; a client that needs the detail can GET it.
    """
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
