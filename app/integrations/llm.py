"""Optional LLM place extraction (Anthropic Claude).

Strictly opt-in. With no ``ANTHROPIC_API_KEY`` the application never touches the
network here and falls back to the deterministic rules in ``gazetteer.py``. That
keeps the "no API keys required" promise while still showing what the better path
looks like when a key exists.

Where an LLM genuinely beats the rules: informal or multilingual text, implicit
references ("the Ferry Building is underwater" -> San Francisco), and messy
punctuation. Where it does not: it is ~500ms instead of ~0ms, it costs money per
call, and it can hallucinate a plausible-looking place. So the design is:

* the LLM only ever extracts a **place name string** - it is never asked for
  coordinates, because inventing a lat/lng is exactly what it would do well
  enough to be dangerous;
* the returned name is then resolved by a real geocoder;
* every result is cached for 24h, keyed by description hash, so repeat calls are
  free;
* any failure falls straight through to the rules.

The HTTP API is called directly rather than through the SDK to avoid adding a
dependency for one request, and to keep the timeout/retry behaviour consistent
with every other integration.
"""

from __future__ import annotations

import json
import re

import structlog

from app.core.config import settings
from app.integrations.http import request_json
from app.integrations.resilience import (
    CircuitOpenError,
    PermanentError,
    TransientError,
    get_breaker,
    retry_async,
)

log = structlog.get_logger(__name__)

PROVIDER = "anthropic"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

_SYSTEM_PROMPT = (
    "You extract geographic locations from emergency incident reports. "
    "Reply with ONLY a JSON object, no prose, no markdown fences, in this exact shape: "
    '{"place": "<most specific place name, or null>", "confidence": <0.0-1.0>}. '
    "Prefer the most specific identifiable place, formatted as "
    '"Neighbourhood, City, Country" when the text supports it. '
    "Return null for place if the text names no real geographic location. "
    "Never invent a location that is not implied by the text."
)


def is_available() -> bool:
    return settings.llm_enabled


async def extract_place(description: str) -> tuple[str | None, float]:
    """Return ``(place_name, confidence)``.

    Raises on failure so the caller can fall back. Returns ``(None, 0.0)`` when
    the model ran successfully and found no location - a real answer, not a fault.
    """
    if not settings.llm_enabled:
        raise TransientError("LLM extraction disabled: no ANTHROPIC_API_KEY configured")

    breaker = get_breaker(PROVIDER)

    async def _call() -> tuple[str | None, float]:
        payload = await request_json(
            "POST",
            _API_URL,
            provider=PROVIDER,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": settings.anthropic_model,
                # Small ceiling: the reply is one short JSON object, and capping
                # output caps both latency and cost.
                "max_tokens": 200,
                "system": _SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Incident report:\n{description[:2000]}",
                    }
                ],
            },
        )
        return _parse(payload)

    try:
        return await breaker.call(
            lambda: retry_async(_call, name=PROVIDER, retry_on=(TransientError,))
        )
    except (CircuitOpenError, TransientError, PermanentError) as exc:
        log.warning("llm_extraction_failed", provider=PROVIDER, error=str(exc))
        raise


def _parse(payload: object) -> tuple[str | None, float]:
    """Pull the JSON object out of the model's reply.

    Models sometimes wrap JSON in prose or a markdown fence despite instructions,
    so the text is scanned for the first JSON object rather than parsed blindly.
    Treating that as a hard failure would make the integration needlessly brittle.
    """
    try:
        blocks = payload["content"]  # type: ignore[index]
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    except (KeyError, TypeError, AttributeError) as exc:
        raise PermanentError(f"{PROVIDER}: unexpected response envelope: {exc}") from exc

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise PermanentError(f"{PROVIDER}: no JSON object in model reply: {text[:120]!r}")

    try:
        parsed = json.loads(match.group(0))
    except ValueError as exc:
        raise PermanentError(f"{PROVIDER}: model reply was not valid JSON: {exc}") from exc

    place = parsed.get("place")
    if place is not None and not isinstance(place, str):
        raise PermanentError(f"{PROVIDER}: 'place' was {type(place).__name__}, expected string")
    place = place.strip() if isinstance(place, str) and place.strip() else None

    try:
        confidence = float(parsed.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    # Never trust a model's self-reported score outside its declared range.
    confidence = min(1.0, max(0.0, confidence))

    return place, (confidence if place else 0.0)
