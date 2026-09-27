"""Location resolution: free-text description -> coordinates."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.cache import cache
from app.core.config import settings
from app.integrations import gazetteer, llm, nominatim

log = structlog.get_logger(__name__)

_NOMINATIM_CONFIDENCE = 0.90
_GAZETTEER_CONFIDENCE = 0.70


@dataclass(frozen=True, slots=True)
class LocationResolution:
    """Outcome of resolving a description. Always returned; never raises."""

    location_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source: str = "unresolved"
    confidence: float | None = None

    @property
    def is_resolved(self) -> bool:
        return self.latitude is not None and self.longitude is not None


UNRESOLVED = LocationResolution()


async def resolve_from_description(description: str) -> LocationResolution:
    """Full pipeline: description -> place name -> coordinates."""
    if not description or not description.strip():
        return UNRESOLVED

    place, extraction_confidence, extraction_method = await _extract_place(description)
    if not place:
        log.info("location_unresolved_no_place", preview=description[:60])
        return UNRESOLVED

    geo = await _geocode(place)
    if geo is None:
        log.info("location_partially_resolved", place=place, method=extraction_method)
        return LocationResolution(
            location_name=place,
            source=f"{extraction_method}+ungeocoded",
            confidence=round(extraction_confidence * 0.5, 2),
        )

    latitude, longitude, display_name, geocoder, geocoder_confidence = geo
    return LocationResolution(
        location_name=display_name or place,
        latitude=latitude,
        longitude=longitude,
        source=f"{extraction_method}+{geocoder}",
        confidence=round(extraction_confidence * geocoder_confidence, 2),
    )


async def resolve_place_name(place: str) -> LocationResolution:
    """Geocode a place name the caller already has (skips extraction)."""
    geo = await _geocode(place)
    if geo is None:
        return LocationResolution(location_name=place, source="ungeocoded", confidence=0.0)
    latitude, longitude, display_name, geocoder, confidence = geo
    return LocationResolution(
        location_name=display_name or place,
        latitude=latitude,
        longitude=longitude,
        source=geocoder,
        confidence=confidence,
    )


# Step 1: extraction


async def _extract_place(description: str) -> tuple[str | None, float, str]:
    """Return (place, confidence, method). Always succeeds; falls back to rules."""
    key = cache.make_key("nlp:place", description)

    cached = await cache.get(key)
    if cached is not None:
        return (
            cached.get("place"),
            float(cached.get("confidence", 0.0)),
            cached.get("method", "cached"),
        )

    result: tuple[str | None, float, str]

    if llm.is_available():
        try:
            place, confidence = await llm.extract_place(description)
            if place:
                result = (place, confidence, "llm")
            else:
                rules = gazetteer.extract_place(description)
                result = (rules.place, rules.confidence, rules.method)
        except Exception as exc:
            log.warning("llm_extraction_fell_back_to_rules", error=str(exc))
            rules = gazetteer.extract_place(description)
            result = (rules.place, rules.confidence, rules.method)
    else:
        rules = gazetteer.extract_place(description)
        result = (rules.place, rules.confidence, rules.method)

    await cache.set(
        key,
        {"place": result[0], "confidence": result[1], "method": result[2]},
        ttl=settings.cache_ttl_place_extract,
    )
    return result


# Step 2: geocoding


async def _geocode(place: str) -> tuple[float, float, str, str, float] | None:
    """Return (lat, lng, display_name, provider, confidence) or None."""
    key = cache.make_key("geocode", place)

    async def _resolve() -> dict[str, object] | None:
        # Primary: the real geocoder.
        if settings.geocoder_enabled:
            try:
                hit = await nominatim.geocode(place)
                if hit is not None:
                    return {
                        "lat": hit.latitude,
                        "lng": hit.longitude,
                        "name": hit.display_name,
                        "provider": "nominatim",
                        "confidence": _NOMINATIM_CONFIDENCE,
                    }
            except Exception as exc:  # noqa: BLE001
                log.warning("geocoder_unavailable_using_gazetteer", place=place, error=str(exc))

        # Fallback: the bundled offline table.
        entry = gazetteer.lookup(place)
        if entry is not None:
            return {
                "lat": entry.latitude,
                "lng": entry.longitude,
                "name": entry.name,
                "provider": "gazetteer",
                "confidence": _GAZETTEER_CONFIDENCE,
            }

        log.info("geocode_unresolved", place=place)
        return None

    try:
        result = await cache.get_or_set(
            key,
            _resolve,
            ttl=settings.cache_ttl_geocode,
            stale_grace=settings.cache_ttl_geocode,
        )
        payload = result.value
    except Exception as exc:
        log.error("geocode_cache_path_failed", place=place, error=str(exc))
        return None

    if not payload:
        return None

    return (
        float(payload["lat"]),
        float(payload["lng"]),
        str(payload["name"]),
        str(payload["provider"]),
        float(payload["confidence"]),
    )
