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
Backend for coordinating disaster response: incident records, automatic location
resolution from free text, geospatial resource search, aggregated community
reports, and a live event stream.

### Getting started

1. `POST /api/v1/auth/login` with `{"username": "admin", "password": "admin123"}`
2. Click **Authorize** above and paste the `access_token`
3. `POST /api/v1/disasters` - omit the coordinates and let the service resolve
   the location from your description

### Demo accounts

| username | role | permissions |
|---|---|---|
| `admin` | admin | full access, including `DELETE` |
| `contributor` | contributor | create; update **only their own** records |
| `responder` | responder | read-only |

### Things worth looking at

* **Geospatial** - `GET /disasters/{id}/resources` uses PostGIS `ST_DWithin`
  against a GiST-indexed `geography(Point, 4326)` column.
* **Caching** - `GET /disasters/{id}/reports` returns `X-Cache: HIT|MISS|STALE`.
* **Failure handling** - stop the mock provider
  (`docker compose stop mock-services`) and re-request reports: you get a `200`
  with `degraded: true`, not a `500`.
* **Real-time** - `curl -N http://localhost:8000/api/v1/events/stream`, then
  create a disaster in another terminal.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop shared resources exactly once per process.

    Connecting eagerly here means a misconfigured Redis surfaces at boot rather
    than on the first request that needs it. Teardown is symmetric so a reload
    does not leak connection pools.
    """
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
        # Loud, but not fatal: refusing to boot would be worse during an incident.
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
    """Build the application.

    A factory rather than a module-level singleton so tests can construct an
    isolated instance with their own settings instead of importing whatever the
    environment happened to produce.
    """
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

    # Middleware runs in REVERSE registration order on the way in, so the last
    # one added is the outermost. Request context must be outermost: everything
    # inside it, including a rate-limit rejection, needs the request id.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,  # bearer tokens, not cookies; no need for credentialed CORS
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Cache", "X-Cache-Age", "X-Degraded",
                        "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-Response-Time-ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    # The demo map client, served by the API so there is no second server and no
    # CORS or file:// problem to work around.
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/demo", StaticFiles(directory=str(web_dir), html=True), name="demo")

        # response_model=None: FastAPI otherwise tries to derive a response model
        # from the return annotation, and a union of Response subclasses is not a
        # valid Pydantic type.
        @app.get("/favicon.ico", include_in_schema=False, response_model=None)
        async def favicon() -> Response:
            icon = web_dir / "favicon.ico"
            if icon.exists():
                return FileResponse(icon)
            # No icon shipped; 204 keeps the browser from retrying every request.
            return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    return app


app = create_app()
