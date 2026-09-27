"""Cache/pubsub backends.

One narrow interface, two implementations:

* :class:`RedisBackend`  - the real thing. Shared across every worker process,
  gives us native TTLs, atomic ``SET NX`` locks for single-flight, atomic
  ``INCR`` for rate limiting, and pub/sub for real-time fan-out.
* :class:`MemoryBackend` - a per-process stand-in used when Redis is unreachable
  and ``CACHE_ALLOW_MEMORY_FALLBACK`` is on, and by the unit tests.

The fallback exists so a missing Redis degrades the system (cache is no longer
shared; events reach only clients on this worker) instead of preventing boot.
That is the right trade for a coordination tool: serving slightly staler data
beats serving nothing. In production you would set the flag to ``false`` and let
the health check take the instance out of rotation instead.

The interface is deliberately string-in/string-out. Serialization is the caller's
concern (``app/cache/cache.py``), which keeps the backends swappable.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)


@runtime_checkable
class CacheBackend(Protocol):
    name: str

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> int: ...
    async def delete_pattern(self, pattern: str) -> int: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def acquire_lock(self, key: str, ttl: int) -> bool: ...
    async def release_lock(self, key: str) -> None: ...
    async def publish(self, channel: str, message: str) -> None: ...
    def subscribe(self, channel: str) -> AsyncIterator[str]: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# In-process backend
# --------------------------------------------------------------------------- #


class MemoryBackend:
    """Dict-backed cache with lazy expiry, plus asyncio-queue pub/sub.

    Expiry is lazy (checked on read) rather than swept by a background task:
    simpler, and for a cache the only cost is memory held slightly too long.
    ``_sweep`` bounds that when the store grows.
    """

    name = "memory"
    _MAX_KEYS = 10_000

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}
        self._lock = asyncio.Lock()

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= time.monotonic()

    def _sweep(self) -> None:
        now = time.monotonic()
        dead = [k for k, (_, exp) in self._store.items() if exp is not None and exp <= now]
        for k in dead:
            self._store.pop(k, None)

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._expired(expires_at):
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        async with self._lock:
            if len(self._store) >= self._MAX_KEYS:
                self._sweep()
            if len(self._store) >= self._MAX_KEYS:
                # Still full after sweeping: drop the oldest insertion. Crude, but
                # this backend is a fallback, not the primary cache.
                self._store.pop(next(iter(self._store)), None)
            expires_at = time.monotonic() + ttl if ttl else None
            self._store[key] = (value, expires_at)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                removed += 1
        return removed

    async def delete_pattern(self, pattern: str) -> int:
        victims = [k for k in list(self._store) if fnmatch.fnmatch(k, pattern)]
        return await self.delete(*victims)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or self._expired(entry[1]):
                self._store[key] = ("1", time.monotonic() + ttl)
                return 1
            current = int(entry[0]) + 1
            self._store[key] = (str(current), entry[1])  # keep the original window
            return current

    async def acquire_lock(self, key: str, ttl: int) -> bool:
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None and not self._expired(entry[1]):
                return False
            self._store[key] = ("1", time.monotonic() + ttl)
            return True

    async def release_lock(self, key: str) -> None:
        self._store.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            # Never block a publisher on a slow consumer: drop instead.
            if not queue.full():
                queue.put_nowait(message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(channel, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subs = self._subscribers.get(channel)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(channel, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._store.clear()
        self._subscribers.clear()


# --------------------------------------------------------------------------- #
# Redis backend
# --------------------------------------------------------------------------- #


class RedisBackend:
    """Async Redis client wrapper.

    ``decode_responses=True`` so the rest of the codebase deals in ``str`` and
    never has to think about bytes.
    """

    name = "redis"

    def __init__(self, client) -> None:  # noqa: ANN001 - redis.asyncio.Redis
        self._client = client

    @property
    def client(self):  # noqa: ANN201
        return self._client

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            await self._client.set(key, value, ex=ttl)
        else:
            await self._client.set(key, value)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*keys))

    async def delete_pattern(self, pattern: str) -> int:
        # SCAN, never KEYS: KEYS blocks the single-threaded Redis event loop for
        # the whole sweep, which is a production outage waiting to happen.
        removed = 0
        batch: list[str] = []
        async for key in self._client.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                removed += await self.delete(*batch)
                batch.clear()
        if batch:
            removed += await self.delete(*batch)
        return removed

    async def incr(self, key: str, ttl: int) -> int:
        # Pipelined so INCR and EXPIRE are one round-trip. EXPIRE is only set on
        # the first hit, which makes this a fixed window rather than a sliding one.
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.ttl(key)
            count, remaining = await pipe.execute()
        if remaining is None or remaining < 0:
            await self._client.expire(key, ttl)
        return int(count)

    async def acquire_lock(self, key: str, ttl: int) -> bool:
        # SET NX EX is atomic: exactly one caller can win, which is what makes
        # the single-flight guarantee in cache.py real rather than best-effort.
        return bool(await self._client.set(key, "1", nx=True, ex=ttl))

    async def release_lock(self, key: str) -> None:
        await self._client.delete(key)

    async def publish(self, channel: str, message: str) -> None:
        await self._client.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    yield message["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

_backend: CacheBackend | None = None


async def init_backend() -> CacheBackend:
    """Connect to Redis, or fall back to memory. Called once from app lifespan."""
    global _backend
    if _backend is not None:
        return _backend

    try:
        from redis.asyncio import Redis

        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
        await client.ping()
        _backend = RedisBackend(client)
        log.info("cache_backend_ready", backend="redis", url=_redact(settings.redis_url))
    except Exception as exc:
        if not settings.cache_allow_memory_fallback:
            raise
        log.warning(
            "cache_backend_fallback",
            backend="memory",
            reason=str(exc),
            note="cache is now per-process; real-time events do not cross workers",
        )
        _backend = MemoryBackend()
    return _backend


def get_backend() -> CacheBackend:
    """Synchronous accessor. Lazily creates a memory backend outside app lifespan
    (unit tests, scripts) so nothing has to special-case startup ordering."""
    global _backend
    if _backend is None:
        _backend = MemoryBackend()
    return _backend


def set_backend(backend: CacheBackend | None) -> None:
    """Test hook for injecting a backend or resetting between tests."""
    global _backend
    _backend = backend


async def close_backend() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None


def _redact(url: str) -> str:
    """Strip credentials before a connection string reaches a log line."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"
