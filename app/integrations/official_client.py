"""Client for the mock official-bulletin provider (bonus feature)."""

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

PROVIDER = "mock-official-api"
SOURCE = "official"

_SEVERITY_TO_PRIORITY = {
    "severe": ReportPriority.CRITICAL,
    "high": ReportPriority.HIGH,
    "moderate": ReportPriority.MEDIUM,
    "low": ReportPriority.LOW,
}


async def fetch_bulletins(region: str, *, limit: int = 5) -> list[Report]:
    breaker = get_breaker(PROVIDER)

    async def _call() -> list[Report]:
        payload = await request_json(
            "GET",
            f"{settings.official_api_base_url.rstrip('/')}/official/bulletins",
            provider=PROVIDER,
            params={"region": region, "limit": limit},
        )
        return _normalise(payload)

    try:
        return await breaker.call(
            lambda: retry_async(_call, name=PROVIDER, retry_on=(TransientError,))
        )
    except CircuitOpenError:
        log.warning("official_fetch_skipped_circuit_open", provider=PROVIDER, region=region)
        raise
    except (TransientError, PermanentError) as exc:
        log.warning("official_fetch_failed", provider=PROVIDER, region=region, error=str(exc))
        raise


def _normalise(payload: Any) -> list[Report]:
    if not isinstance(payload, dict):
        raise PermanentError(f"{PROVIDER}: expected a JSON object, got {type(payload).__name__}")

    raw_bulletins = payload.get("bulletins")
    if not isinstance(raw_bulletins, list):
        raise PermanentError(f"{PROVIDER}: 'bulletins' missing or not a list")

    reports: list[Report] = []
    for raw in raw_bulletins:
        if not isinstance(raw, dict):
            continue
        headline = (raw.get("headline") or "").strip()
        body = (raw.get("body") or "").strip()
        if not headline and not body:
            continue
        try:
            severity = str(raw.get("severity", "")).lower()
            reports.append(
                Report(
                    id=str(raw.get("ref") or f"official-{abs(hash(headline)) % 10**8}"),
                    source=SOURCE,
                    provider=PROVIDER,
                    author=str(raw.get("agency") or "Unknown agency"),
                    content=f"{headline}. {body}".strip(". ").strip(),
                    created_at=_parse_timestamp(raw.get("issued_at")),
                    verified=True,
                    priority=_SEVERITY_TO_PRIORITY.get(severity, ReportPriority.MEDIUM),
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("official_item_skipped", provider=PROVIDER, error=str(exc))

    return reports


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            log.debug("official_timestamp_unparseable", value=value[:40])
    return datetime.now(UTC)
