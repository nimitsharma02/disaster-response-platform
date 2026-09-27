"""Two-tier cache with stale-while-revalidate and single-flight."""

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

_LOCK_TTL = 10
_LOCK_WAIT_TOTAL = 2.0
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


# L1

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
            _l1.clear()
    _l1[key] = (value, time.monotonic() + ttl)


def _l1_drop(key: str) -> None:
    _l1.pop(key, None)


def clear_l1() -> None:
    """Test hook, the L1 tier is module state and must not leak across tests."""
    _l1.clear()


# Keys and envelopes


def make_key(namespace: str, *parts: Any) -> str:
    """Build a namespaced key."""
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
    """Return (value, age_seconds, soft_ttl) or None if the entry is unreadable."""
    try:
        envelope = json.loads(raw)
        return envelope["v"], max(0.0, time.time() - float(envelope["t"])), int(envelope["ttl"])
    except (ValueError, KeyError, TypeError):
        return None


# Public API


async def get_or_set(
    key: str,
    loader: Callable[[], Awaitable[T]],
    *,
    ttl: int,
    stale_grace: int | None = None,
    l1_ttl: int | None = None,
    force_refresh: bool = False,
) -> CacheResult[T]:
    """Read key, calling loader only when necessary."""
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
    """Drop specific keys."""
    for key in keys:
        _l1_drop(key)
    return await get_backend().delete(*keys)


async def invalidate_namespace(namespace: str, *parts: Any) -> int:
    prefix = make_key(namespace, *parts) if parts else f"drp:{namespace}"
    for key in [k for k in list(_l1) if k.startswith(prefix)]:
        _l1_drop(key)
    return await get_backend().delete_pattern(f"{prefix}*")
