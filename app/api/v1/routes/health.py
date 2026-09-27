"""Health and readiness endpoints.

Two probes, because they answer different questions:

* ``/health`` (**liveness**) - is this process running? Touches nothing external.
  If it fails, restart the container.
* ``/health/ready`` (**readiness**) - can this instance serve traffic? Checks the
  database and cache. If it fails, stop routing to this instance but do **not**
  restart it: a database blip is not fixed by killing the app.

Conflating them is a classic operational mistake - a brief database outage then
triggers a restart loop across every instance at once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response, status

from app.cache.backend import get_backend
from app.core.config import settings
from app.db.session import check_database
from app.integrations.resilience import all_breakers
from app.schemas.common import HealthStatus, ReadinessStatus

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("", response_model=HealthStatus, summary="Liveness probe")
async def health() -> HealthStatus:
    return HealthStatus(
        status="ok",
        version=settings.app_version,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/ready",
    response_model=ReadinessStatus,
    summary="Readiness probe: dependency checks",
)
async def readiness(response: Response) -> ReadinessStatus:
    """Check every dependency and report per-component status.

    Returns 503 when a hard dependency is down, so an orchestrator or load
    balancer can act on the status code alone without parsing the body.

    The cache is a **soft** dependency: with ``CACHE_ALLOW_MEMORY_FALLBACK`` the
    app still serves correctly without Redis, just without a shared cache or
    cross-worker events. It is reported as degraded, not failed.
    """
    checks: dict[str, Any] = {}

    database_ok = await check_database()
    checks["database"] = {
        "status": "ok" if database_ok else "down",
        "required": True,
        "engine": "postgresql+postgis",
    }

    backend = get_backend()
    cache_ok = await backend.ping()
    checks["cache"] = {
        "status": "ok" if cache_ok else "down",
        "required": False,
        "backend": backend.name,
        "note": (
            None
            if backend.name == "redis"
            else "using in-process fallback: cache is not shared and events do not cross workers"
        ),
    }

    # Breaker state is genuinely useful here: it shows at a glance which
    # external provider is currently being skipped and why.
    checks["external_providers"] = {
        name: breaker.snapshot() for name, breaker in all_breakers().items()
    } or {"note": "no external calls made yet this process"}

    checks["geocoder"] = {
        "enabled": settings.geocoder_enabled,
        "provider": "nominatim" if settings.geocoder_enabled else "gazetteer-only",
        "llm_extraction": "enabled" if settings.llm_enabled else "disabled (no API key)",
    }

    if not database_ok:
        overall = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not cache_ok:
        overall = "degraded"
    else:
        overall = "ok"

    return ReadinessStatus(status=overall, checks=checks, timestamp=datetime.now(UTC))
