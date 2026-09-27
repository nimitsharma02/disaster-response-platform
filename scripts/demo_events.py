"""Minimal real-time client, for demonstrating the event stream from a terminal.

    docker compose exec api python -m scripts.demo_events

Then, in another terminal, create or update a disaster and watch events arrive.
Uses SSE rather than WebSocket so it needs no extra client library.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

BASE_URL = os.getenv("DEMO_BASE_URL", "http://localhost:8000")
STREAM = f"{BASE_URL}/api/v1/events/stream"


async def main() -> None:
    print(f"Connecting to {STREAM} ...")
    print("Waiting for events. Create or update a disaster to see one. Ctrl-C to stop.\n")

    # timeout=None on read: an SSE stream is meant to stay open indefinitely.
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client,
        client.stream("GET", STREAM) as response,
    ):
            response.raise_for_status()
            event_name = "message"
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line.split(":", 1)[1].strip())
                    data = payload.get("data", {})
                    print(f"[{payload.get('ts', '')}] {event_name}")
                    print(json.dumps(data, indent=2))
                    print("-" * 60)
                elif line.startswith(":"):
                    print(".", end="", flush=True)  # heartbeat


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
        sys.exit(0)
