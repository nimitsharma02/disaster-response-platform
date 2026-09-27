"""v1 API router."""

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
