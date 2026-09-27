"""Client for the mock social-media provider."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import settings
from app.db.models.enums import ReportPriority
from app.integrations.http import request_json
from app.integrations.resilience import (
    CircuitOpenError,
    PermanentError,
    TransientError,
    get_breaker,
    retry_async,
)
from app.schemas.report import Report

log = structlog.get_logger(__name__)

PROVIDER = "mock-social-api"
SOURCE = "social"


async def fetch_reports(query: str, *, limit: int = 12) -> list[Report]:
    """Fetch and normalise social reports for query."""
    breaker = get_breaker(PROVIDER)

    async def _call() -> list[Report]:
        payload = await request_json(
            "GET",
            f"{settings.social_api_base_url.rstrip('/')}/social/search",
            provider=PROVIDER,
            params={"q": query, "limit": limit},
        )
        return _normalise(payload)

    try:
        return await breaker.call(
            lambda: retry_async(_call, name=PROVIDER, retry_on=(TransientError,))
        )
    except CircuitOpenError:
        log.warning("social_fetch_skipped_circuit_open", provider=PROVIDER, query=query)
        raise
    except (TransientError, PermanentError) as exc:
        log.warning("social_fetch_failed", provider=PROVIDER, query=query, error=str(exc))
        raise


def _normalise(payload: Any) -> list[Report]:
    """Map the provider's shape onto ours, discarding items we cannot read."""
    if not isinstance(payload, dict):
        raise PermanentError(f"{PROVIDER}: expected a JSON object, got {type(payload).__name__}")

    raw_posts = payload.get("posts")
    if not isinstance(raw_posts, list):
        raise PermanentError(f"{PROVIDER}: 'posts' missing or not a list")

    reports: list[Report] = []
    skipped = 0

    for raw in raw_posts:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        content = (raw.get("content") or "").strip()
        if not content:
            skipped += 1
            continue
        try:
            reports.append(
                Report(
                    id=str(raw.get("post_id") or f"social-{abs(hash(content)) % 10**8}"),
                    source=SOURCE,
                    provider=PROVIDER,
                    author=str(raw.get("user") or "anonymous"),
                    content=content,
                    created_at=_parse_timestamp(raw.get("created_at")),
                    verified=bool(raw.get("verified_account", False)),
                    priority=ReportPriority.LOW,
                )
            )
        except Exception as exc:
            skipped += 1
            log.debug("social_item_skipped", provider=PROVIDER, error=str(exc))

    if skipped:
        log.info("social_items_skipped", provider=PROVIDER, skipped=skipped, kept=len(reports))
    return reports


def _parse_timestamp(value: Any) -> datetime:
    """Parse a timestamp leniently, defaulting to now."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            log.debug("social_timestamp_unparseable", value=value[:40])
    return datetime.now(UTC)
