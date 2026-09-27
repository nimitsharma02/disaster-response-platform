"""v1 API router.

Versioning is in the URL prefix (``/api/v1``) rather than a header. It is
greppable, cacheable, and trivial to route in a proxy; a v2 becomes a second
router mounted alongside this one rather than a fork of every handler.

``/disasters/{id}/resources`` and ``/disasters/{id}/reports`` are registered from
their own modules rather than from ``disasters.py``, so each router owns the
endpoints for its domain. They do not collide with ``/disasters/{id}``: Starlette
matches full path patterns, and a different segment count cannot match. Order
would only matter for a *literal* segment competing with a parameter - something
like ``/disasters/search`` versus ``/disasters/{id}``, where the literal must be
registered first. There is no such route here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import auth, disasters, events, health, reports, resources

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(resources.router)
api_router.include_router(reports.router)
api_router.include_router(disasters.router)
api_router.include_router(events.router)
