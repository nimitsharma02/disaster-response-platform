"""Shared schema building blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")

Latitude = Annotated[float, Field(ge=-90, le=90, description="Degrees north, WGS 84 (EPSG:4326)")]
Longitude = Annotated[float, Field(ge=-180, le=180, description="Degrees east, WGS 84 (EPSG:4326)")]


class ORMModel(BaseModel):
    """Base for models populated from SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class GeoPoint(BaseModel):
    """A resolved coordinate pair."""

    latitude: Latitude
    longitude: Longitude

    @computed_field  # type: ignore[prop-decorator]
    @property
    def geojson(self) -> dict[str, Any]:
        """GeoJSON representation, for clients that want to feed a map directly."""
        return {"type": "Point", "coordinates": [self.longitude, self.latitude]}


class PageMeta(BaseModel):
    """Pagination envelope metadata."""

    total: int = Field(description="Total rows matching the filter, ignoring pagination")
    limit: int
    offset: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        return self.offset + self.limit < self.total


class Page(BaseModel, Generic[T]):
    """Standard list envelope."""

    items: list[T]
    meta: PageMeta


class PaginationParams(BaseModel):
    """Offset pagination."""

    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ProblemDetail(BaseModel):
    """RFC 9457 error body. Declared so it appears in the OpenAPI document."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "https://docs.drp.local/errors/not_found",
                "title": "Resource not found",
                "status": 404,
                "detail": "Disaster 7c9e6679-7425-40de-944b-e07fc1f90ae7 does not exist",
                "instance": "/api/v1/disasters/7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "code": "not_found",
                "request_id": "018f3c2e-9b1a-7c3d-8e4f-1a2b3c4d5e6f",
            }
        }
    )

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    request_id: str | None = None
    errors: list[dict[str, Any]] | None = None


class HealthStatus(BaseModel):
    status: str
    version: str
    environment: str
    timestamp: datetime


class ReadinessStatus(BaseModel):
    """Readiness is about dependencies; liveness is about the process."""

    status: str
    checks: dict[str, Any]
    timestamp: datetime
