"""Real-time endpoints: WebSocket and Server-Sent Events."""

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

_HEARTBEAT_SECONDS = 20


@router.get(
    "/stream",
    summary="Server-Sent Events stream of disaster events",
    response_class=StreamingResponse,
)
async def sse_stream() -> StreamingResponse:
    """Subscribe to live events over SSE."""

    async def _generate() -> AsyncIterator[str]:
        yield _format(events.build_envelope("connected", {"transport": "sse"}))

        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)

        async def _pump() -> None:
            async for event in events.subscribe():
                if queue.full():
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
            "X-Accel-Buffering": "no",
        },
    )


def _format(event: dict) -> str:
    """Render one event in the SSE wire format."""
    return (
        f"id: {event.get('id', '')}\n"
        f"event: {event.get('event', 'message')}\n"
        f"data: {json.dumps(event, default=str)}\n\n"
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Subscribe to live events over WebSocket."""
    await websocket.accept()
    log.info("websocket_client_connected", client=str(websocket.client))

    await websocket.send_json(events.build_envelope("connected", {"transport": "websocket"}))

    async def _forward() -> None:
        async for event in events.subscribe():
            await websocket.send_json(event)

    async def _receive() -> None:
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
