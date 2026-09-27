"""Real-time endpoints: WebSocket and Server-Sent Events.

Both transports carry the same envelope from the same Redis pub/sub channel.
Two are offered because they suit different consumers:

* **WebSocket** (``/events/ws``) - bidirectional, used by the browser map client.
  It supports a ping/pong so the client can prove the link is alive.
* **SSE** (``/events/stream``) - one-directional, plain HTTP, and verifiable with
  a single ``curl -N``. It reconnects automatically in browsers, needs no client
  library, and passes through proxies that mishandle WebSocket upgrades.

Envelope::

    {"id": "...", "event": "disaster_updated", "ts": "2026-09-27T...", "data": {...}}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.realtime import events

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/events", tags=["Real-time"])

# Sent when the stream is otherwise idle. Without it, an idle connection is
# indistinguishable from a dead one and proxies will quietly drop it.
_HEARTBEAT_SECONDS = 20


@router.get(
    "/stream",
    summary="Server-Sent Events stream of disaster events",
    response_class=StreamingResponse,
)
async def sse_stream() -> StreamingResponse:
    """Subscribe to live events over SSE.

    Verify it with::

        curl -N http://localhost:8000/api/v1/events/stream

    then create or update a disaster in another terminal and watch the event
    arrive. A ``: heartbeat`` comment line is emitted every 20s to keep
    intermediaries from closing an idle connection.
    """

    async def _generate() -> AsyncIterator[str]:
        # Tell the client what it is connected to before anything happens, so an
        # empty stream is obviously "connected and quiet" rather than "broken".
        yield _format(events.build_envelope("connected", {"transport": "sse"}))

        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)

        async def _pump() -> None:
            async for event in events.subscribe():
                if queue.full():
                    # Drop rather than block the shared subscriber: one slow
                    # client must not stall delivery to everyone else.
                    log.warning("sse_queue_full_dropping_event", event_type=event.get("event"))
                    continue
                await queue.put(event)

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                    yield _format(event)
                except TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            # Normal: the client disconnected.
            raise
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            log.info("sse_client_disconnected")

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tell nginx not to buffer, which would defeat streaming entirely.
            "X-Accel-Buffering": "no",
        },
    )


def _format(event: dict) -> str:
    """Render one event in the SSE wire format.

    ``id:`` lets a browser resume with Last-Event-ID; ``event:`` lets a client
    register a handler per event type rather than switching on the payload.
    """
    return (
        f"id: {event.get('id', '')}\n"
        f"event: {event.get('event', 'message')}\n"
        f"data: {json.dumps(event, default=str)}\n\n"
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Subscribe to live events over WebSocket.

    The client may send ``ping`` and will receive ``pong``; anything else is
    ignored. Reads and the event pump run as concurrent tasks so a client that
    never sends anything still receives events, and a disconnect tears both down.
    """
    await websocket.accept()
    log.info("websocket_client_connected", client=str(websocket.client))

    await websocket.send_json(events.build_envelope("connected", {"transport": "websocket"}))

    async def _forward() -> None:
        async for event in events.subscribe():
            await websocket.send_json(event)

    async def _receive() -> None:
        # Draining incoming frames is what surfaces a disconnect; without it a
        # dead client is only noticed on the next send.
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_json(events.build_envelope("pong", {}))

    forward_task = asyncio.create_task(_forward())
    receive_task = asyncio.create_task(_receive())

    try:
        done, pending = await asyncio.wait(
            {forward_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            # Re-raise so a genuine failure is logged rather than swallowed.
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("websocket_error", error=str(exc))
    finally:
        for task in (forward_task, receive_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("websocket_client_disconnected", client=str(websocket.client))
