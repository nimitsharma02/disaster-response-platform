"""Resource schemas, including the nearby-search parameters."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
from app.db.models.enums import ResourceType
from app.schemas.common import GeoPoint, Latitude, Longitude, ORMModel


class ResourceCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "Bellevue Hospital Center",
                "type": "hospital",
                "latitude": 40.7392,
                "longitude": -73.9762,
                "address": "462 1st Ave, New York, NY 10016",
                "capacity": 850,
            }
        },
    )

    name: str = Field(min_length=2, max_length=200)
    type: ResourceType
    latitude: Latitude
    longitude: Longitude
    address: str | None = Field(default=None, max_length=500)
    capacity: int | None = Field(default=None, ge=0, le=1_000_000)
    contact: str | None = Field(default=None, max_length=120)
    disaster_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class ResourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=200)
    type: ResourceType | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    address: str | None = Field(default=None, max_length=500)
    capacity: int | None = Field(default=None, ge=0, le=1_000_000)
    contact: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class ResourceResponse(ORMModel):
    id: uuid.UUID
    name: str
    type: ResourceType
    location: GeoPoint
    address: str | None = None
    capacity: int | None = None
    contact: str | None = None
    is_active: bool
    disaster_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    distance_km: float | None = Field(
        default=None, description="Great-circle distance from the query point, kilometres"
    )


class NearbySearchParams(BaseModel):
    """Parameters for GET /disasters/{id}/resources."""

    lat: Latitude | None = None
    lng: Longitude | None = None
    radius_km: float = Field(default=settings.default_radius_km, gt=0)
    type: ResourceType | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("radius_km")
    @classmethod
    def _cap_radius(cls, v: float) -> float:
        if v > settings.max_radius_km:
            raise ValueError(f"radius_km must not exceed {settings.max_radius_km}")
        return v


class NearbySearchResponse(BaseModel):
    """Echoes the effective search parameters alongside the results."""

    center: GeoPoint
    center_source: str = Field(description="'query' if supplied by the caller, else 'disaster'")
    radius_km: float
    count: int
    items: list[ResourceResponse]
