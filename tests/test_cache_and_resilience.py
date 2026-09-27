"""Unit tests for the cache layer and the resilience primitives."""

from __future__ import annotations

import asyncio

import pytest

from app.cache import cache
from app.integrations.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    PermanentError,
    RateLimiter,
    TransientError,
    retry_async,
)

pytestmark = pytest.mark.asyncio


# Cache


async def test_loader_runs_once_then_serves_from_cache():
    calls = {"n": 0}

    async def loader() -> dict:
        calls["n"] += 1
        return {"value": calls["n"]}

    key = cache.make_key("test", "basic")

    first = await cache.get_or_set(key, loader, ttl=60)
    assert first.status == "MISS"
    assert first.value == {"value": 1}
    assert calls["n"] == 1

    second = await cache.get_or_set(key, loader, ttl=60)
    assert second.status == "HIT"
    assert second.value == {"value": 1}
    assert calls["n"] == 1, "a cache hit must not call the loader"


async def test_force_refresh_bypasses_the_cache():
    calls = {"n": 0}

    async def loader() -> dict:
        calls["n"] += 1
        return {"value": calls["n"]}

    key = cache.make_key("test", "force")
    await cache.get_or_set(key, loader, ttl=60)
    result = await cache.get_or_set(key, loader, ttl=60, force_refresh=True)

    assert calls["n"] == 2
    assert result.value == {"value": 2}


async def test_soft_expiry_makes_a_value_stale_but_reusable():
    """Past ttl the value is stale; within stale_grace it can still be served."""
    async def loader() -> str:
        return "original"

    key = cache.make_key("test", "stale")
    await cache.get_or_set(key, loader, ttl=0, stale_grace=3600)
    cache.clear_l1()

    async def failing_loader() -> str:
        raise TransientError("origin is down")

    result = await cache.get_or_set(key, failing_loader, ttl=0, stale_grace=3600)

    assert result.status == "STALE"
    assert result.value == "original"
    assert result.degraded is True


async def test_origin_failure_with_a_cold_cache_raises_upstream_error():
    from app.core.errors import UpstreamError

    async def failing_loader() -> str:
        raise TransientError("origin is down")

    with pytest.raises(UpstreamError):
        await cache.get_or_set(cache.make_key("test", "cold"), failing_loader, ttl=60)


async def test_single_flight_collapses_a_concurrent_stampede():
    """Twenty concurrent callers on a cold key must produce one origin call."""
    calls = {"n": 0}

    async def slow_loader() -> int:
        calls["n"] += 1
        await asyncio.sleep(0.1)
        return calls["n"]

    key = cache.make_key("test", "stampede")
    results = await asyncio.gather(
        *(cache.get_or_set(key, slow_loader, ttl=60) for _ in range(20))
    )

    assert calls["n"] == 1, f"expected a single origin call, got {calls['n']}"
    assert {r.value for r in results} == {1}


async def test_invalidate_removes_the_entry():
    async def loader() -> str:
        return "value"

    key = cache.make_key("test", "invalidate")
    await cache.get_or_set(key, loader, ttl=60)
    assert await cache.get(key) == "value"

    await cache.invalidate(key)
    assert await cache.get(key) is None


async def test_keys_are_namespaced_and_long_inputs_are_hashed():
    """Keys must not collide across namespaces or blow past Redis limits."""
    assert cache.make_key("geocode", "Mumbai") == "drp:geocode:mumbai"
    assert cache.make_key("reports", "abc") != cache.make_key("geocode", "abc")

    long_key = cache.make_key("nlp:place", "x" * 500)
    assert len(long_key) < 120, "an over-long component must be hashed"

    assert cache.make_key("nlp:place", "x" * 500) == long_key


async def test_a_corrupt_cache_entry_is_treated_as_a_miss():
    """A cache must never be able to break the application."""
    from app.cache.backend import get_backend

    key = cache.make_key("test", "corrupt")
    await get_backend().set(key, "this is not json at all", ttl=60)
    cache.clear_l1()

    assert await cache.get(key) is None

    async def loader() -> str:
        return "recovered"

    result = await cache.get_or_set(key, loader, ttl=60)
    assert result.value == "recovered"


# Retry


async def test_retry_succeeds_after_a_transient_failure():
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientError("temporary")
        return "ok"

    result = await retry_async(flaky, name="test", max_attempts=5, base_delay=0.001)

    assert result == "ok"
    assert attempts["n"] == 3


async def test_retry_gives_up_after_the_attempt_limit():
    attempts = {"n": 0}

    async def always_fails() -> str:
        attempts["n"] += 1
        raise TransientError("always down")

    with pytest.raises(TransientError):
        await retry_async(always_fails, name="test", max_attempts=3, base_delay=0.001)

    assert attempts["n"] == 3, "max_attempts counts total calls, not extra retries"


async def test_permanent_errors_are_never_retried():
    """A 4xx will fail identically every time; retrying only adds latency."""
    attempts = {"n": 0}

    async def bad_request() -> str:
        attempts["n"] += 1
        raise PermanentError("400 bad request")

    with pytest.raises(PermanentError):
        await retry_async(bad_request, name="test", max_attempts=5, base_delay=0.001)

    assert attempts["n"] == 1


# Circuit breaker


async def test_breaker_opens_after_the_failure_threshold():
    breaker = CircuitBreaker(name="test", fail_threshold=3, reset_timeout=60)

    async def failing() -> None:
        raise TransientError("down")

    for _ in range(3):
        with pytest.raises(TransientError):
            await breaker.call(failing)

    assert breaker.state is CircuitState.OPEN

    calls = {"n": 0}

    async def should_not_run() -> None:
        calls["n"] += 1

    with pytest.raises(CircuitOpenError):
        await breaker.call(should_not_run)
    assert calls["n"] == 0


async def test_a_success_resets_the_failure_count():
    """Consecutive failures trip the breaker; isolated blips must not accumulate."""
    breaker = CircuitBreaker(name="test", fail_threshold=3, reset_timeout=60)

    async def failing() -> None:
        raise TransientError("down")

    async def succeeding() -> str:
        return "ok"

    for _ in range(2):
        with pytest.raises(TransientError):
            await breaker.call(failing)

    await breaker.call(succeeding)
    assert breaker.state is CircuitState.CLOSED

    for _ in range(2):
        with pytest.raises(TransientError):
            await breaker.call(failing)

    assert breaker.state is CircuitState.CLOSED, "the counter should have reset on success"


async def test_breaker_half_opens_after_the_reset_timeout_and_closes_on_success():
    breaker = CircuitBreaker(name="test", fail_threshold=2, reset_timeout=0.05)

    async def failing() -> None:
        raise TransientError("down")

    async def succeeding() -> str:
        return "recovered"

    for _ in range(2):
        with pytest.raises(TransientError):
            await breaker.call(failing)
    assert breaker.state is CircuitState.OPEN

    await asyncio.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN

    assert await breaker.call(succeeding) == "recovered"
    assert breaker.state is CircuitState.CLOSED


async def test_a_failed_probe_reopens_the_breaker():
    """Half-open admits one trial; if it fails, the timer restarts."""
    breaker = CircuitBreaker(name="test", fail_threshold=2, reset_timeout=0.05)

    async def failing() -> None:
        raise TransientError("still down")

    for _ in range(2):
        with pytest.raises(TransientError):
            await breaker.call(failing)

    await asyncio.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN

    with pytest.raises(TransientError):
        await breaker.call(failing)

    assert breaker.state is CircuitState.OPEN


async def test_permanent_errors_do_not_trip_the_breaker():
    """A 4xx means our request was wrong, not that the dependency is down."""
    breaker = CircuitBreaker(name="test", fail_threshold=2, reset_timeout=60)

    async def bad_request() -> None:
        raise PermanentError("400")

    for _ in range(5):
        with pytest.raises(PermanentError):
            await breaker.call(bad_request)

    assert breaker.state is CircuitState.CLOSED


# Rate limiter


async def test_rate_limiter_paces_calls():
    """Nominatim allows 1 request/second; the limiter must actually enforce it."""
    limiter = RateLimiter(rate_per_sec=20, burst=1)

    started = asyncio.get_event_loop().time()
    for _ in range(4):
        await limiter.acquire()
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed >= 0.12, f"expected pacing, took only {elapsed:.3f}s"
