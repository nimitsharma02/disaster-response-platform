"""Location resolution: free-text description -> coordinates.

    "Heavy flooding has affected Manhattan, NYC."
                    |
                    v
      STEP 1  extract a place name
              LLM (only if ANTHROPIC_API_KEY is set)  --fails--> rule-based
                    |                                            extractor
                    v
              "Manhattan, NYC"
                    |
                    v
      STEP 2  resolve to coordinates
              cache --miss--> Nominatim --fails/no match--> bundled gazetteer
                    |                                            |
                    v                                            v
              (40.7831, -73.9712)                          still nothing?
                                                                 |
                                                                 v
                                                        location = NULL,
                                                        source = "unresolved"

Design decisions worth stating plainly:

**Extraction and geocoding are separate steps.** It is tempting to ask an LLM for
coordinates directly. That is a bad idea: a model will happily produce a
confident, plausible, wrong lat/lng, and nothing downstream can tell. So the
model is only ever asked for a *name*, and a real geocoder turns names into
points. Each component does the thing it can actually be held accountable for.

**Failure is never fatal.** Every step degrades to a weaker one, and the final
fallback is to record the incident with no location at all. Rejecting an incident
report because a third-party geocoder was down would be the wrong behaviour for
a disaster tool.

**Both steps are cached, with very different TTLs.** Extraction is keyed by a
hash of the description (24h; the mapping is deterministic, the TTL only bounds
prompt changes). Geocoding is keyed by the place name (7 days; a city does not
move, and this is what keeps us inside Nominatim's 1 req/s limit).

**Confidence is multiplicative.** A 0.55 extraction feeding a 0.9 geocode is a
~0.5 result overall - uncertainty compounds, and the number should say so.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.cache import cache
from app.core.config import settings
from app.integrations import gazetteer, llm, nominatim

log = structlog.get_logger(__name__)

# Geocoder confidence by provider. Nominatim is a real geocoding service; the
# bundled gazetteer holds approximate city centroids, and is scored accordingly.
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
        # We know what the place is called but not where it is. Keep the name -
        # it is still useful to an operator, and a later re-resolve may succeed.
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


# --------------------------------------------------------------------------- #
# Step 1: extraction
# --------------------------------------------------------------------------- #


async def _extract_place(description: str) -> tuple[str | None, float, str]:
    """Return ``(place, confidence, method)``. Always succeeds; falls back to rules."""
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
                # The model ran and found nothing. Still try the rules: they use
                # a gazetteer the model does not have.
                rules = gazetteer.extract_place(description)
                result = (rules.place, rules.confidence, rules.method)
        except Exception as exc:  # noqa: BLE001 - any LLM failure falls through to rules
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


# --------------------------------------------------------------------------- #
# Step 2: geocoding
# --------------------------------------------------------------------------- #


async def _geocode(place: str) -> tuple[float, float, str, str, float] | None:
    """Return ``(lat, lng, display_name, provider, confidence)`` or ``None``.

    The cache wraps the *whole* decision including the gazetteer fallback, so a
    period of Nominatim downtime does not cause every request to re-attempt a
    call that is going to fail.
    """
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
                # A clean "no such place" - fall through to the gazetteer, which
                # may still recognise an alias Nominatim does not.
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
            # A geocode is immutable enough that an old value is always better
            # than none, so allow a long stale window.
            stale_grace=settings.cache_ttl_geocode,
        )
        payload = result.value
    except Exception as exc:  # noqa: BLE001 - resolution must never break a write
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
