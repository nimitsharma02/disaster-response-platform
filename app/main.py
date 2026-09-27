"""Application entry point: factory, lifespan and middleware wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.cache.backend import close_backend, init_backend
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import dispose_engine
from app.integrations.http import close_http_client, init_http_client

configure_logging()
log = structlog.get_logger(__name__)

DESCRIPTION = """
Backend for coordinating disaster response: incident records, location resolution
from free text, geospatial resource search, community reports and live updates.

Getting started:

1. `POST /api/v1/auth/login` with `{"username": "admin", "password": "admin123"}`
2. Click Authorize above and paste the `access_token`
3. `POST /api/v1/disasters` without coordinates, the location is resolved from
   the description

Demo accounts:

- `admin` / `admin123` - full access including DELETE
- `contributor` / `contributor123` - can create, and update only their own records
- `responder` / `responder123` - read only

Worth a look:

- `GET /disasters/{id}/resources` uses PostGIS ST_DWithin on a GiST indexed
  geography column
- `GET /disasters/{id}/reports` returns an X-Cache header of HIT, MISS or STALE
- Stop the mock provider with `docker compose stop mock-services` and request
  reports again, you get a 200 with `degraded: true` instead of a 500
- `curl -N http://localhost:8000/api/v1/events/stream` for the live event feed
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop shared resources exactly once per process."""
    log.info(
        "application_starting",
        version=settings.app_version,
        environment=settings.app_env,
        llm_extraction=settings.llm_enabled,
        geocoder=settings.geocoder_enabled,
    )

    backend = await init_backend()
    await init_http_client()

    if settings.is_production and settings.jwt_secret == "dev-only-insecure-secret-change-me":
        log.error(
            "insecure_jwt_secret_in_production",
            action="set JWT_SECRET to a strong random value immediately",
        )

    log.info("application_ready", cache_backend=backend.name)
    try:
        yield
    finally:
        log.info("application_stopping")
        await close_http_client()
        await close_backend()
        await dispose_engine()
        log.info("application_stopped")


def create_app() -> FastAPI:
    """Build the application."""
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
        contact={"name": "API support", "email": "support@example.com"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Cache", "X-Cache-Age", "X-Degraded",
                        "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-Response-Time-ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/demo", StaticFiles(directory=str(web_dir), html=True), name="demo")

        @app.get("/favicon.ico", include_in_schema=False, response_model=None)
        async def favicon() -> Response:
            icon = web_dir / "favicon.ico"
            if icon.exists():
                return FileResponse(icon)
            return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    return app


app = create_app()
