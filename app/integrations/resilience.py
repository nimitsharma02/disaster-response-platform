"""Resilience primitives for outbound calls: retry, circuit breaker,."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

T = TypeVar("T")


class TransientError(Exception):
    """Failure that is worth retrying (timeout, connection reset, 5xx)."""


class PermanentError(Exception):
    """Failure that will recur identically (4xx, malformed payload). Never retried."""


class CircuitOpenError(Exception):
    """The breaker is open; the call was rejected without being attempted."""


# Retry


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    name: str,
    max_attempts: int | None = None,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    retry_on: tuple[type[Exception], ...] = (TransientError,),
) -> T:
    """Call operation, retrying transient failures with jittered backoff."""
    attempts = (max_attempts if max_attempts is not None else settings.http_max_retries + 1)
    attempts = max(1, attempts)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last_error = exc
            if attempt == attempts:
                break
            window = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = random.uniform(0, window)
            log.warning(
                "retrying_after_transient_error",
                provider=name,
                attempt=attempt,
                of=attempts,
                sleep_s=round(delay, 3),
                error=str(exc),
            )
            await asyncio.sleep(delay)
        except PermanentError:
            raise

    log.error("retries_exhausted", provider=name, attempts=attempts, error=str(last_error))
    raise TransientError(f"{name}: {attempts} attempt(s) failed: {last_error}") from last_error


# Circuit breaker


class CircuitState(StrEnum):
    CLOSED = "closed"        # normal: calls pass through
    OPEN = "open"            # failing: calls rejected immediately
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-dependency breaker."""

    name: str
    fail_threshold: int = field(default_factory=lambda: settings.circuit_fail_threshold)
    reset_timeout: float = field(default_factory=lambda: settings.circuit_reset_timeout_s)

    _state: CircuitState = CircuitState.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _half_open_in_flight: bool = False

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and (
            time.monotonic() - self._opened_at >= self.reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = False
            log.info("circuit_half_open", provider=self.name)
        return self._state

    def allows_request(self) -> bool:
        current = self.state
        if current is CircuitState.CLOSED:
            return True
        if current is CircuitState.OPEN:
            return False
        # HALF_OPEN: admit exactly one probe.
        if self._half_open_in_flight:
            return False
        self._half_open_in_flight = True
        return True

    def record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            log.info("circuit_closed", provider=self.name)
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is CircuitState.HALF_OPEN:
            self._trip("probe_failed")
            return
        if self._failures >= self.fail_threshold:
            self._trip("threshold_reached")

    def _trip(self, reason: str) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._half_open_in_flight = False
        log.error(
            "circuit_opened",
            provider=self.name,
            reason=reason,
            failures=self._failures,
            retry_in_s=self.reset_timeout,
        )

    def reset(self) -> None:
        """Test hook, breakers are process-global and must not leak across tests."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        if not self.allows_request():
            raise CircuitOpenError(
                f"{self.name} circuit is open; not attempting the call"
            )
        try:
            result = await operation()
        except (PermanentError, CircuitOpenError):
            self._half_open_in_flight = False
            raise
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result

    def snapshot(self) -> dict[str, object]:
        return {
            "state": str(self.state),
            "consecutive_failures": self._failures,
            "fail_threshold": self.fail_threshold,
        }


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name)
    return _breakers[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    return dict(_breakers)


def reset_all_breakers() -> None:
    for breaker in _breakers.values():
        breaker.reset()


# Rate limiter


class RateLimiter:
    """Async token bucket, used to honour a provider's published quota."""

    def __init__(self, rate_per_sec: float, burst: int = 1) -> None:
        self._rate = max(rate_per_sec, 0.001)
        self._capacity = max(burst, 1)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1.0
