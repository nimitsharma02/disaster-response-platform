"""Mock external providers: a social-media feed and an official bulletin feed.

This runs as its **own process on its own port**, not as an in-process stub. That
matters: it means the main application's timeouts, retries, circuit breaker and
cache are exercised against a real socket that can really be slow, really return
500, and really be stopped mid-demo (``docker compose stop mock-services``). An
in-process fake would prove none of that.

Two deliberately *different* payload shapes are served, so the normalisation
layer in the main app has something real to do:

    social   -> {"content", "user", "created_at", "post_id", "likes"}
    official -> {"headline", "body", "issued_at", "agency", "severity", "ref"}

Responses are deterministic for a given query (seeded by a hash of it), so cache
hit/miss behaviour is observable rather than masked by random content.

Chaos knobs, for demonstrating failure handling:

    POST /_control/chaos?failure_rate=0.8&latency_ms=3000
    POST /_control/chaos?failure_rate=0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response

app = FastAPI(
    title="Mock External Services",
    description="Stand-in for a social-media API and an official bulletin feed.",
    version="1.0.0",
)


class Chaos:
    """Runtime-tunable failure injection."""

    def __init__(self) -> None:
        self.failure_rate = float(os.getenv("MOCK_FAILURE_RATE", "0.0"))
        self.latency_ms = int(os.getenv("MOCK_LATENCY_MS", "120"))

    async def apply(self, endpoint: str) -> None:
        # Simulated network latency, so the caller's timeout budget is real.
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.failure_rate > 0 and random.random() < self.failure_rate:
            # 503 rather than 500: it is the honest code for "overloaded", and it
            # is what the client's retry-then-trip-breaker path should react to.
            raise HTTPException(status_code=503, detail=f"{endpoint} is temporarily overloaded")


chaos = Chaos()


# --------------------------------------------------------------------------- #
# Content templates
# --------------------------------------------------------------------------- #

_SOCIAL_TEMPLATES = [
    ("Need drinking water near {place}, we have been without supply since morning", "high"),
    ("URGENT: family of four trapped on the second floor in {place}, water still rising",
     "critical"),
    ("Power has been out across {place} for over 14 hours now", "medium"),
    ("Volunteers distributing food packets at the community hall in {place}", "low"),
    ("Anyone know if the relief camp in {place} is still accepting people?", "medium"),
    ("Medical help needed in {place} - elderly neighbour is diabetic and out of insulin",
     "critical"),
    ("Roads into {place} are completely blocked, do not attempt to drive in", "high"),
    ("Mobile network is patchy in {place} but we are all safe here", "low"),
    ("Shelter at the school in {place} has reached capacity, they are turning people away", "high"),
    ("My grandmother is missing since yesterday evening in {place}, please share", "critical"),
    ("Clean-up crews have started work in the eastern part of {place}", "low"),
    ("Baby formula and diapers urgently needed at the {place} relief centre", "high"),
    ("Water level dropping slowly in {place}, some streets now passable", "low"),
    ("Gas leak reported near the market in {place}, area being evacuated", "critical"),
]

_OFFICIAL_TEMPLATES = [
    ("Evacuation advisory issued for low-lying areas of {place}",
     "Residents in low-lying wards of {place} are advised to move to designated relief centres. "
     "Transport is being arranged from the main junction every 30 minutes.", "high"),
    ("Relief camps operational in {place}",
     "Twelve relief camps are now operational across {place} with capacity for 4,800 people. "
     "Medical teams are stationed at each site.", "moderate"),
    ("Water supply restoration under way in {place}",
     "Crews have restored supply to roughly 60 percent of affected households in {place}. "
     "Full restoration is expected within 48 hours.", "moderate"),
    ("Helpline activated for {place}",
     "A 24-hour helpline has been activated for residents of {place}. "
     "Report missing persons and request medical assistance through this line.", "low"),
    ("Red alert continues for {place}",
     "The meteorological department has extended the red alert for {place} for a further 24 hours. "
     "Further heavy rainfall is expected.", "severe"),
]

_USERNAMES = [
    "citizen123", "relief_vol_22", "localnews_now", "ward7_resident", "helping_hands",
    "on_the_ground", "nearby_witness", "mutualaid_net", "rescue_watch", "community_alert",
]

_AGENCIES = [
    "National Disaster Management Authority",
    "State Emergency Operations Centre",
    "Municipal Corporation",
    "Meteorological Department",
]


def _seeded(query: str) -> random.Random:
    """Deterministic RNG per query, so the same request returns the same payload."""
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _place_from_query(query: str) -> str:
    cleaned = query.replace("disaster", "").replace("emergency", "").strip(" ,-")
    if not cleaned:
        return "the affected area"
    # Only title-case input that is entirely lowercase. Applying .title() to an
    # already-cased name mangles it - "Manhattan, New York, USA" would come back
    # as "Manhattan, New York, Usa".
    return cleaned.title() if cleaned.islower() else cleaned


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health", summary="Liveness probe")
async def health() -> dict[str, Any]:
    # Intentionally not subject to chaos: a health check that lies is worse than
    # no health check.
    return {
        "status": "ok",
        "service": "mock-external-services",
        "failure_rate": chaos.failure_rate,
        "latency_ms": chaos.latency_ms,
    }


@app.get("/social/search", summary="Mock social-media search")
async def social_search(
    response: Response,
    q: str = Query(min_length=1, description="Free-text query, normally the place name"),
    limit: int = Query(default=12, ge=1, le=50),
) -> dict[str, Any]:
    """Raw social payload. Note the provider-specific field names."""
    await chaos.apply("social search")

    rng = _seeded(f"social:{q}")
    place = _place_from_query(q)
    now = datetime.now(UTC)

    templates = _SOCIAL_TEMPLATES.copy()
    rng.shuffle(templates)

    posts = []
    for index, (template, _hint) in enumerate(templates[:limit]):
        posted_at = now - timedelta(minutes=rng.randint(2, 720))
        posts.append(
            {
                "post_id": f"sp_{hashlib.sha1(f'{q}{index}'.encode()).hexdigest()[:10]}",
                "content": template.format(place=place),
                "user": rng.choice(_USERNAMES),
                "created_at": posted_at.isoformat(),
                "likes": rng.randint(0, 900),
                "reshares": rng.randint(0, 200),
                "verified_account": rng.random() < 0.15,
            }
        )

    # A response the client can use to be a good citizen, mirroring real APIs.
    response.headers["X-RateLimit-Remaining"] = str(rng.randint(40, 100))
    return {"query": q, "result_count": len(posts), "posts": posts}


@app.get("/official/bulletins", summary="Mock official bulletin feed")
async def official_bulletins(
    region: str = Query(min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict[str, Any]:
    """Raw official payload - a completely different shape from the social one."""
    await chaos.apply("official bulletins")

    rng = _seeded(f"official:{region}")
    place = _place_from_query(region)
    now = datetime.now(UTC)

    templates = _OFFICIAL_TEMPLATES.copy()
    rng.shuffle(templates)

    bulletins = []
    for index, (headline, body, severity) in enumerate(templates[:limit]):
        issued = now - timedelta(minutes=rng.randint(10, 1440))
        bulletins.append(
            {
                "ref": f"BUL-{hashlib.sha1(f'{region}{index}'.encode()).hexdigest()[:8].upper()}",
                "headline": headline.format(place=place),
                "body": body.format(place=place),
                "issued_at": issued.isoformat(),
                "agency": rng.choice(_AGENCIES),
                "severity": severity,
            }
        )

    return {"region": region, "count": len(bulletins), "bulletins": bulletins}


@app.post("/_control/chaos", summary="Tune failure injection (test hook)")
async def set_chaos(
    failure_rate: float = Query(default=0.0, ge=0.0, le=1.0),
    latency_ms: int = Query(default=120, ge=0, le=30_000),
) -> dict[str, Any]:
    chaos.failure_rate = failure_rate
    chaos.latency_ms = latency_ms
    return {"failure_rate": chaos.failure_rate, "latency_ms": chaos.latency_ms}
