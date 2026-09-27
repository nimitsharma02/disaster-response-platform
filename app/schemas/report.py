"""Community report schemas.

Reports are *not* stored in our database - they are fetched from external
providers, normalised into this shape, classified, and cached. The normalised
model is the contract our clients depend on, which is what lets a provider's
payload change without breaking them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import ReportPriority


class Report(BaseModel):
    """One normalised community or official report.

    Every provider gets flattened into this. The mock social service returns
    ``{content, user, created_at}``; the official bulletin feed returns
    ``{headline, body, issued_at, agency}``. Normalising at the boundary means
    the rest of the system never branches on provider.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "social-8f21",
                "source": "social",
                "provider": "mock-social-api",
                "author": "citizen123",
                "content": "Need drinking water near Manhattan, we have been without since 6am",
                "created_at": "2026-09-27T09:14:00Z",
                "priority": "high",
                "priority_score": 0.72,
                "matched_signals": ["need_water", "duration_mentioned"],
                "verified": False,
            }
        }
    )

    id: str
    source: str = Field(description="'social' or 'official'")
    provider: str = Field(description="Which upstream produced this")
    author: str
    content: str
    created_at: datetime
    location_hint: str | None = None
    verified: bool = False

    # Assigned by services/classification.py, not by the provider.
    priority: ReportPriority = ReportPriority.LOW
    priority_score: float = Field(default=0.0, ge=0, le=1)
    matched_signals: list[str] = Field(
        default_factory=list,
        description="Which rules fired. Present so the ranking is auditable rather "
                    "than an opaque score.",
    )


class ReportsMeta(BaseModel):
    """Everything the caller needs to judge how much to trust this payload.

    A degraded response returns 200, not 502: stale reports are far more useful
    to a responder than an error page. But it must say so, loudly and in-band -
    silently serving stale data as if it were live would be the actual failure.
    """

    cache: str = Field(description="HIT | MISS | STALE | BYPASS")
    age_seconds: float = Field(description="Age of the underlying data")
    degraded: bool = Field(
        default=False,
        description="True when at least one provider failed and cached or fallback "
                    "data was served instead",
    )
    sources: dict[str, str] = Field(
        default_factory=dict,
        description="Per-provider outcome: ok | cached | failed | circuit_open",
    )
    total_fetched: int = 0
    counts_by_priority: dict[str, int] = Field(default_factory=dict)


class ReportsResponse(BaseModel):
    disaster_id: str
    items: list[Report]
    meta: ReportsMeta
