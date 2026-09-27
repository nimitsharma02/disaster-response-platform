"""Two-tier cache with stale-while-revalidate and single-flight.

    request
       |
       v
    L1  in-process dict (CACHE_L1_TTL, ~30s)  --hit--> return  (X-Cache: HIT)
       | miss
       v
    L2  Redis envelope                        --fresh-> return  (X-Cache: HIT)
       | miss or soft-expired
       v
    single-flight lock (SET NX)
       |  won                       |  lost
       v                            v
    call origin                 serve stale if we have it,
       |  ok      |  failed     else wait briefly for the winner
       v          v
    store+return  serve stale (X-Cache: STALE, degraded=true)
                  or raise UpstreamError if nothing cached

Three ideas are doing the real work here:

**Soft vs hard expiry.** The envelope records when it was fetched. ``ttl`` is the
*soft* expiry - past it the value is stale but still usable. The Redis key itself
lives for ``ttl + stale_grace``. So when the origin is down we can answer from
stale data and flag ``degraded: true`` instead of returning a 502. For disaster
reports, five-minute-old data is enormously more useful than an error.

**Single-flight.** On a cold key, N concurrent requests would otherwise all call
the origin at once (a cache stampede) - exactly when the origin is least able to
cope. One caller wins an atomic ``SET NX`` lock and fetches; the rest serve stale
or wait for the winner.

**L1.** The in-process tier is not really about Redis latency; it collapses the
burst of identical calls a single page render produces into one Redis round-trip.
It is per-worker and short-lived, so cross-worker inconsistency is bounded by
``CACHE_L1_TTL``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

import structlog

from app.cache.backend import get_backend
from app.core.config import settings
from app.core.errors import UpstreamError

log = structlog.get_logger(__name__)

T = TypeVar("T")

CacheStatus = Literal["HIT", "MISS", "STALE", "BYPASS"]

_LOCK_TTL = 10           # seconds a refresh may hold the single-flight lock
_LOCK_WAIT_TOTAL = 2.0   # how long a loser waits for the winner before self-serving
_LOCK_POLL = 0.05


@dataclass(slots=True)
class CacheResult(Generic[T]):
    """A cached value plus everything the caller needs to be honest about it."""

    value: T
    status: CacheStatus
    age_seconds: float
    degraded: bool = False

    @property
    def is_stale(self) -> bool:
        return self.status == "STALE"


# --------------------------------------------------------------------------- #
# L1
# --------------------------------------------------------------------------- #

_l1: dict[str, tuple[Any, float]] = {}
_L1_MAX = 2_000


def _l1_get(key: str) -> Any | None:
    entry = _l1.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at <= time.monotonic():
        _l1.pop(key, None)
        return None
    return value


def _l1_set(key: str, value: Any, ttl: int) -> None:
    if ttl <= 0:
        return
    if len(_l1) >= _L1_MAX:
        now = time.monotonic()
        for k in [k for k, (_, exp) in _l1.items() if exp <= now]:
            _l1.pop(k, None)
        if len(_l1) >= _L1_MAX:
            _l1.clear()  # fallback: an L1 flush costs one extra Redis read, nothing more
    _l1[key] = (value, time.monotonic() + ttl)


def _l1_drop(key: str) -> None:
    _l1.pop(key, None)


def clear_l1() -> None:
    """Test hook - the L1 tier is module state and must not leak across tests."""
    _l1.clear()


# --------------------------------------------------------------------------- #
# Keys and envelopes
# --------------------------------------------------------------------------- #


def make_key(namespace: str, *parts: Any) -> str:
    """Build a namespaced key.

    Long or user-supplied parts are hashed so a key can never blow past Redis
    limits or smuggle in a delimiter and collide with a different namespace.
    """
    rendered: list[str] = []
    for part in parts:
        text = str(part).strip().lower()
        if len(text) > 80 or any(c in text for c in " \n\r\t:"):
            text = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        rendered.append(text)
    return ":".join(["drp", namespace, *rendered])


def _pack(value: Any, ttl: int) -> str:
    return json.dumps({"v": value, "t": time.time(), "ttl": ttl}, separators=(",", ":"))


def _unpack(raw: str) -> tuple[Any, float, int] | None:
    """Return ``(value, age_seconds, soft_ttl)`` or None if the entry is unreadable."""
    try:
        envelope = json.loads(raw)
        return envelope["v"], max(0.0, time.time() - float(envelope["t"])), int(envelope["ttl"])
    except (ValueError, KeyError, TypeError):
        # A corrupt or legacy entry is treated as a miss rather than an error.
        return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def get_or_set(
    key: str,
    loader: Callable[[], Awaitable[T]],
    *,
    ttl: int,
    stale_grace: int | None = None,
    l1_ttl: int | None = None,
    force_refresh: bool = False,
) -> CacheResult[T]:
    """Read ``key``, calling ``loader`` only when necessary.

    ``loader`` is an async callable with no arguments (close over what it needs).
    It is called at most once per key per refresh cycle across all workers.
    """
    backend = get_backend()
    grace = settings.cache_stale_grace if stale_grace is None else stale_grace
    l1_seconds = settings.cache_l1_ttl if l1_ttl is None else l1_ttl

    if force_refresh:
        _l1_drop(key)
    else:
        cached = _l1_get(key)
        if cached is not None:
            return CacheResult(value=cached, status="HIT", age_seconds=0.0)

        raw = await backend.get(key)
        unpacked = _unpack(raw) if raw else None
        if unpacked is not None:
            value, age, soft_ttl = unpacked
            if age < soft_ttl:
                _l1_set(key, value, min(l1_seconds, max(1, int(soft_ttl - age))))
                return CacheResult(value=value, status="HIT", age_seconds=age)

    # Miss or stale: exactly one caller should hit the origin.
    lock_key = f"{key}:lock"
    have_lock = await backend.acquire_lock(lock_key, _LOCK_TTL)

    if not have_lock:
        stale = await _read_stale(key)
        if stale is not None:
            value, age = stale
            log.debug("cache_stale_while_refreshing", key=key, age=round(age, 1))
            return CacheResult(value=value, status="STALE", age_seconds=age, degraded=False)
        refreshed = await _await_winner(key, l1_seconds)
        if refreshed is not None:
            return refreshed
        # The winner died or is too slow. Fetching ourselves is a bounded
        # duplicate call, which beats failing the request.
        have_lock = False

    try:
        started = time.perf_counter()
        value = await loader()
        await backend.set(key, _pack(value, ttl), ttl=ttl + grace)
        _l1_set(key, value, min(l1_seconds, ttl))
        log.debug(
            "cache_filled", key=key, ttl=ttl,
            ms=round((time.perf_counter() - started) * 1000),
        )
        return CacheResult(value=value, status="MISS", age_seconds=0.0)
    except Exception as exc:
        # The origin failed. This is the whole point of the stale window.
        stale = await _read_stale(key)
        if stale is not None:
            value, age = stale
            log.warning(
                "cache_serving_stale_after_origin_failure",
                key=key,
                age=round(age, 1),
                error=str(exc),
            )
            return CacheResult(value=value, status="STALE", age_seconds=age, degraded=True)
        log.error("cache_origin_failed_no_fallback", key=key, error=str(exc))
        if isinstance(exc, UpstreamError):
            raise
        raise UpstreamError(
            f"Upstream call failed and no cached value is available: {exc}"
        ) from exc
    finally:
        if have_lock:
            await backend.release_lock(lock_key)


async def _read_stale(key: str) -> tuple[Any, float] | None:
    """Return a soft-expired but not yet hard-expired value, if one exists."""
    raw = await get_backend().get(key)
    if not raw:
        return None
    unpacked = _unpack(raw)
    if unpacked is None:
        return None
    value, age, _ = unpacked
    return value, age


async def _await_winner(key: str, l1_ttl: int) -> CacheResult[Any] | None:
    """Poll briefly for the lock winner to publish its result."""
    backend = get_backend()
    deadline = time.monotonic() + _LOCK_WAIT_TOTAL
    while time.monotonic() < deadline:
        await asyncio.sleep(_LOCK_POLL)
        raw = await backend.get(key)
        if raw:
            unpacked = _unpack(raw)
            if unpacked is not None:
                value, age, _ = unpacked
                _l1_set(key, value, l1_ttl)
                return CacheResult(value=value, status="HIT", age_seconds=age)
    return None


async def get(key: str) -> Any | None:
    """Plain read. Returns None for a miss and for a soft-expired entry."""
    cached = _l1_get(key)
    if cached is not None:
        return cached
    raw = await get_backend().get(key)
    if not raw:
        return None
    unpacked = _unpack(raw)
    if unpacked is None:
        return None
    value, age, soft_ttl = unpacked
    return value if age < soft_ttl else None


async def set(key: str, value: Any, *, ttl: int, stale_grace: int | None = None) -> None:
    grace = settings.cache_stale_grace if stale_grace is None else stale_grace
    await get_backend().set(key, _pack(value, ttl), ttl=ttl + grace)
    _l1_set(key, value, min(settings.cache_l1_ttl, ttl))


async def invalidate(*keys: str) -> int:
    """Drop specific keys.

    Targeted invalidation, never a flush: a flush would also evict the geocoding
    cache, whose entries cost a rate-limited external call to rebuild.
    """
    for key in keys:
        _l1_drop(key)
    return await get_backend().delete(*keys)


async def invalidate_namespace(namespace: str, *parts: Any) -> int:
    prefix = make_key(namespace, *parts) if parts else f"drp:{namespace}"
    for key in [k for k in list(_l1) if k.startswith(prefix)]:
        _l1_drop(key)
    return await get_backend().delete_pattern(f"{prefix}*")
