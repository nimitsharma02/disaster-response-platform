"""OpenStreetMap Nominatim geocoding client."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.core.config import settings
from app.integrations.http import request_json
from app.integrations.resilience import (
    CircuitOpenError,
    PermanentError,
    RateLimiter,
    TransientError,
    get_breaker,
    retry_async,
)

log = structlog.get_logger(__name__)

PROVIDER = "nominatim"

_limiter = RateLimiter(rate_per_sec=settings.nominatim_rate_limit_per_sec, burst=1)


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    latitude: float
    longitude: float
    display_name: str
    provider: str = PROVIDER
    importance: float | None = None


async def geocode(place: str) -> GeocodeResult | None:
    """Resolve a place name to coordinates."""
    if not settings.geocoder_enabled:
        raise TransientError("geocoder disabled by configuration")

    query = place.strip()
    if not query:
        return None

    breaker = get_breaker(PROVIDER)

    async def _call() -> GeocodeResult | None:
        await _limiter.acquire()
        payload = await request_json(
            "GET",
            f"{settings.nominatim_base_url.rstrip('/')}/search",
            provider=PROVIDER,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 0,
            },
            headers={"User-Agent": settings.nominatim_user_agent},
        )
        if not isinstance(payload, list) or not payload:
            return None

        top = payload[0]
        try:
            return GeocodeResult(
                latitude=float(top["lat"]),
                longitude=float(top["lon"]),
                display_name=top.get("display_name") or query,
                importance=float(top["importance"]) if "importance" in top else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentError(f"{PROVIDER}: unexpected response shape: {exc}") from exc

    try:
        result = await breaker.call(
            lambda: retry_async(_call, name=PROVIDER, retry_on=(TransientError,))
        )
    except CircuitOpenError:
        log.warning("geocode_skipped_circuit_open", provider=PROVIDER, place=query)
        raise
    except (TransientError, PermanentError) as exc:
        log.warning("geocode_failed", provider=PROVIDER, place=query, error=str(exc))
        raise

    if result is None:
        log.info("geocode_no_match", provider=PROVIDER, place=query)
    else:
        log.debug("geocode_ok", provider=PROVIDER, place=query, match=result.display_name)
    return result
