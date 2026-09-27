"""Optional LLM place extraction (Anthropic Claude)."""

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
    """Return (place_name, confidence)."""
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
    """Pull the JSON object out of the model's reply."""
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
    confidence = min(1.0, max(0.0, confidence))

    return place, (confidence if place else 0.0)
