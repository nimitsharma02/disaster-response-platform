"""Disaster request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.enums import DisasterStatus
from app.schemas.common import GeoPoint, Latitude, Longitude, ORMModel

Tag = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")]


def _normalise_tags(tags: list[str] | None) -> list[str]:
    """Lowercase, trim, replace spaces, and de-duplicate while keeping order."""
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        tag = raw.strip().lower().replace(" ", "-")
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


class DisasterCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Manhattan Flash Flooding",
                "description": "Heavy flooding has affected Manhattan, NYC. "
                               "Several subway stations are submerged.",
                "tags": ["flood", "urgent"],
                "status": "reported",
            }
        },
    )

    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=10_000)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    status: DisasterStatus = DisasterStatus.REPORTED

    latitude: Latitude | None = None
    longitude: Longitude | None = None
    location_name: str | None = Field(default=None, max_length=300)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> Any:
        return _normalise_tags(v) if isinstance(v, list) else v

    @field_validator("title", "description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # min_length alone would accept "      ".
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @model_validator(mode="after")
    def _coords_are_a_pair(self) -> DisasterCreate:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class DisasterUpdate(BaseModel):
    """PATCH body."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, min_length=10, max_length=10_000)
    tags: list[Tag] | None = Field(default=None, max_length=20)
    status: DisasterStatus | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    location_name: str | None = Field(default=None, max_length=300)
    reresolve_location: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> Any:
        return _normalise_tags(v) if isinstance(v, list) else v

    @model_validator(mode="after")
    def _at_least_one_field(self) -> DisasterUpdate:
        if not self.model_fields_set - {"reresolve_location"}:
            raise ValueError("at least one field must be provided")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class DisasterCreator(ORMModel):
    id: uuid.UUID
    username: str
    role: str


class LocationInfo(BaseModel):
    """Resolved location plus its provenance."""

    name: str | None = None
    point: GeoPoint | None = None
    source: str | None = Field(
        default=None,
        description="How the location was derived: llm+nominatim, rules+nominatim, "
                    "rules+gazetteer, manual, or unresolved",
    )
    confidence: float | None = Field(default=None, ge=0, le=1)

    @property
    def is_resolved(self) -> bool:
        return self.point is not None


class DisasterResponse(ORMModel):
    id: uuid.UUID
    title: str
    description: str
    location: LocationInfo
    tags: list[str]
    status: DisasterStatus
    created_by: uuid.UUID
    creator: DisasterCreator | None = None
    created_at: datetime
    updated_at: datetime
    # Populated only by proximity-filtered list queries.
    distance_km: float | None = None


class DisasterListFilters(BaseModel):
    """Query filters for GET /disasters."""

    tag: str | None = Field(default=None, description="Exact tag match, e.g. ?tag=flood")
    status: DisasterStatus | None = None
    q: str | None = Field(default=None, min_length=2, max_length=100,
                          description="Case-insensitive substring of title or description")
    created_by: uuid.UUID | None = None
    lat: Latitude | None = None
    lng: Longitude | None = None
    radius_km: float | None = Field(default=None, gt=0, le=20_000)
    sort: str = Field(default="-created_at",
                      description="created_at | -created_at | title | -title | distance")

    @field_validator("tag")
    @classmethod
    def _norm_tag(cls, v: str | None) -> str | None:
        return v.strip().lower().replace(" ", "-") if v else v

    @model_validator(mode="after")
    def _geo_params_are_coherent(self) -> DisasterListFilters:
        provided = [p is not None for p in (self.lat, self.lng)]
        if any(provided) and not all(provided):
            raise ValueError("lat and lng must be provided together")
        if self.radius_km is not None and self.lat is None:
            raise ValueError("radius_km requires lat and lng")
        if self.sort == "distance" and self.lat is None:
            raise ValueError("sort=distance requires lat and lng")
        return self
