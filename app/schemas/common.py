"""Shared schema building blocks.

Everything crossing the HTTP boundary is a Pydantic model. That gives one
definition that simultaneously validates input, serializes output, and generates
the OpenAPI document - so the docs cannot drift from the implementation, because
they are derived from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")

# Latitude and longitude are validated at the type level, so an impossible
# coordinate is rejected by the framework before any handler code runs and
# never reaches PostGIS.
Latitude = Annotated[float, Field(ge=-90, le=90, description="Degrees north, WGS 84 (EPSG:4326)")]
Longitude = Annotated[float, Field(ge=-180, le=180, description="Degrees east, WGS 84 (EPSG:4326)")]


class ORMModel(BaseModel):
    """Base for models populated from SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class GeoPoint(BaseModel):
    """A resolved coordinate pair.

    Exposed as explicit ``latitude``/``longitude`` rather than a GeoJSON
    ``[lng, lat]`` array. GeoJSON's longitude-first ordering is a notorious
    source of transposed-coordinate bugs; named fields make the mistake
    impossible for an API consumer to make silently.
    """

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
    """Standard list envelope.

    A bare JSON array is a dead end: there is nowhere to put a total, a cursor,
    or a degraded-mode flag without a breaking change. Wrapping from day one
    costs one level of nesting and buys unlimited room to grow.
    """

    items: list[T]
    meta: PageMeta


class PaginationParams(BaseModel):
    """Offset pagination.

    Offset (not cursor) is a deliberate simplification: it supports arbitrary
    sort orders and random page access, which is what a small operator-facing
    dataset needs. It degrades on deep pages because the database must still
    walk the skipped rows - noted as a trade-off in the README.

    ``limit`` is capped at 100 so a single request cannot be used to pull the
    whole table.
    """

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
    """Readiness is about dependencies; liveness is about the process.

    Kept separate so an orchestrator restarts a hung process but merely stops
    routing traffic to an instance whose database is briefly unreachable.
    """

    status: str
    checks: dict[str, Any]
    timestamp: datetime
