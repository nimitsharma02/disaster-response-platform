"""Seed the database with demo users, resources and incidents."""

from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db.models.disaster import Disaster
from app.db.models.enums import DisasterStatus, ResourceType, UserRole
from app.db.models.resource import Resource
from app.db.models.user import User
from app.db.session import dispose_engine, get_sessionmaker
from app.repositories import disaster_repo, resource_repo

configure_logging()
log = structlog.get_logger("seed")


USERS = [
    ("admin", "Aisha Rahman (Coordinator)", UserRole.ADMIN, settings.seed_admin_password),
    ("contributor", "Diego Santos (Field Contributor)",
     UserRole.CONTRIBUTOR, settings.seed_contributor_password),
    ("contributor2", "Mei Chen (Field Contributor)", UserRole.CONTRIBUTOR,
     settings.seed_contributor_password),
    ("responder", "Sam Okoro (Responder)", UserRole.RESPONDER, settings.seed_responder_password),
]


NYC = (40.7128, -74.0060)
MUMBAI = (19.0760, 72.8777)

RESOURCES = [
    ("NYC Emergency Command Post", ResourceType.RESCUE, 40.7130, -74.0055, "Broadway, NY", 40),
    ("Downtown Water Distribution", ResourceType.WATER, 40.7155, -74.0090, "Chambers St, NY", 5000),
    ("City Hall Food Bank", ResourceType.FOOD, 40.7127, -74.0059, "City Hall Park, NY", 1200),
    ("Tribeca Emergency Shelter", ResourceType.SHELTER, 40.7195, -74.0089, "Hudson St, NY", 300),
    ("NY Downtown Hospital", ResourceType.HOSPITAL, 40.7107, -74.0051, "William St, NY", 180),
    ("Battery Park Evacuation Point", ResourceType.EVACUATION_POINT, 40.7033, -74.0170,
     "Battery Park, NY", 2000),
    ("SoHo Medical Supply Depot", ResourceType.MEDICAL_SUPPLY, 40.7233, -74.0030, "Spring St, NY",
     None),
    ("Bellevue Hospital Center", ResourceType.HOSPITAL, 40.7392, -73.9762, "1st Ave, NY", 850),
    ("Midtown Relief Shelter", ResourceType.SHELTER, 40.7549, -73.9840, "W 42nd St, NY", 600),
    ("Central Park Staging Area", ResourceType.RESCUE, 40.7812, -73.9665, "Central Park, NY", 100),
    ("Harlem Community Kitchen", ResourceType.FOOD, 40.8116, -73.9465, "Lenox Ave, NY", 800),
    ("Brooklyn Heights Shelter", ResourceType.SHELTER, 40.6959, -73.9954,
     "Clark St, Brooklyn", 450),
    ("Brooklyn Methodist Hospital", ResourceType.HOSPITAL, 40.6680, -73.9790, "6th St, Brooklyn",
     650),
    ("Queens Water Point", ResourceType.WATER, 40.7282, -73.7949, "Queens Blvd, NY", 3000),
    ("Bronx Relief Centre", ResourceType.SHELTER, 40.8448, -73.8648, "Grand Concourse, Bronx", 500),
    ("Staten Island Rescue Base", ResourceType.RESCUE, 40.5795, -74.1502, "Bay St, SI", 60),
    ("Newark Regional Hospital", ResourceType.HOSPITAL, 40.7357, -74.1724, "Newark, NJ", 700),
    ("Jersey City Food Depot", ResourceType.FOOD, 40.7178, -74.0431, "Jersey City, NJ", 900),

    # Mumbai
    ("Mumbai Disaster Control Room", ResourceType.RESCUE, 19.0765, 72.8780, "Fort, Mumbai", 80),
    ("Colaba Water Tanker Point", ResourceType.WATER, 19.0728, 72.8826, "Colaba, Mumbai", 4000),
    ("JJ Hospital", ResourceType.HOSPITAL, 18.9633, 72.8342, "Byculla, Mumbai", 1350),
    ("Dadar Community Shelter", ResourceType.SHELTER, 19.0178, 72.8478, "Dadar, Mumbai", 700),
    ("Andheri Relief Kitchen", ResourceType.FOOD, 19.1197, 72.8468, "Andheri, Mumbai", 1500),
    ("Bandra Evacuation Point", ResourceType.EVACUATION_POINT, 19.0596, 72.8295, "Bandra, Mumbai",
     2500),
    ("KEM Hospital", ResourceType.HOSPITAL, 19.0022, 72.8420, "Parel, Mumbai", 1800),
    ("Powai Medical Depot", ResourceType.MEDICAL_SUPPLY, 19.1176, 72.9060, "Powai, Mumbai", None),
    ("Thane Rescue Station", ResourceType.RESCUE, 19.2183, 72.9781, "Thane, Mumbai", 120),
    ("Navi Mumbai Shelter", ResourceType.SHELTER, 19.0330, 73.0297, "Vashi, Navi Mumbai", 900),

    ("London Emergency Depot", ResourceType.RESCUE, 51.5074, -0.1278, "London, UK", 200),
    ("Tokyo Evacuation Centre", ResourceType.EVACUATION_POINT, 35.6762, 139.6503, "Tokyo, Japan",
     5000),
    ("Kathmandu Field Hospital", ResourceType.HOSPITAL, 27.7172, 85.3240, "Kathmandu, Nepal", 300),
    ("Chennai Flood Shelter", ResourceType.SHELTER, 13.0827, 80.2707, "Chennai, India", 800),
    ("Dhaka Water Point", ResourceType.WATER, 23.8103, 90.4125, "Dhaka, Bangladesh", 6000),
]


DISASTERS = [
    (
        "Manhattan Flash Flooding",
        "Heavy flooding has affected Manhattan, NYC. Several subway stations are "
        "submerged and residents in low-lying streets have been asked to move to "
        "higher floors.",
        ["flood", "urgent", "infrastructure"],
        DisasterStatus.IN_PROGRESS,
        "admin",
    ),
    (
        "Mumbai Monsoon Waterlogging",
        "Severe waterlogging reported across Mumbai after 300mm of rainfall overnight. "
        "Local trains are suspended and several low-lying suburbs are cut off.",
        ["flood", "monsoon"],
        DisasterStatus.VERIFIED,
        "contributor",
    ),
    (
        "Brooklyn Warehouse Fire",
        "A large fire has broken out at a warehouse in Brooklyn. Smoke is drifting "
        "across nearby residential blocks and the fire service has requested an "
        "evacuation radius.",
        ["fire", "evacuation"],
        DisasterStatus.IN_PROGRESS,
        "contributor",
    ),
    (
        "Kathmandu Earthquake Aftershocks",
        "A magnitude 5.8 aftershock struck near Kathmandu this morning. Several "
        "older buildings in the historic district have sustained structural damage.",
        ["earthquake", "structural"],
        DisasterStatus.REPORTED,
        "contributor2",
    ),
    (
        "Chennai Cyclone Landfall",
        "Cyclone made landfall near Chennai overnight with sustained winds of 120 km/h. "
        "Power is out across large parts of the city and coastal areas have been evacuated.",
        ["cyclone", "power-outage", "evacuation"],
        DisasterStatus.VERIFIED,
        "admin",
    ),
    (
        "Dhaka Riverbank Erosion",
        "Progressive riverbank erosion in Dhaka has displaced around 200 families over "
        "the past week. Temporary shelters are being arranged.",
        ["erosion", "displacement"],
        DisasterStatus.RESOLVED,
        "contributor2",
    ),
]


async def seed() -> None:
    factory = get_sessionmaker()

    async with factory() as session:
        # users
        users: dict[str, User] = {}
        for username, display_name, role, password in USERS:
            existing = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()

            if existing:
                existing.password_hash = hash_password(password)
                existing.role = role
                existing.display_name = display_name
                existing.is_active = True
                users[username] = existing
            else:
                user = User(
                    username=username,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    role=role,
                )
                session.add(user)
                users[username] = user

        await session.flush()
        log.info("users_seeded", count=len(users))

        # resources
        created_resources = 0
        for name, resource_type, lat, lng, address, capacity in RESOURCES:
            existing = (
                await session.execute(select(Resource).where(Resource.name == name))
            ).scalar_one_or_none()
            if existing:
                continue
            await resource_repo.create(
                session,
                name=name,
                resource_type=resource_type,
                latitude=lat,
                longitude=lng,
                address=address,
                capacity=capacity,
            )
            created_resources += 1

        await session.flush()
        log.info("resources_seeded", created=created_resources, total=len(RESOURCES))

        # disasters
        from app.services import geocoding_service

        created_disasters = 0
        for title, description, tags, status, owner in DISASTERS:
            existing = (
                await session.execute(select(Disaster).where(Disaster.title == title))
            ).scalar_one_or_none()
            if existing:
                continue

            resolution = await geocoding_service.resolve_from_description(description)
            await disaster_repo.create(
                session,
                title=title,
                description=description,
                tags=tags,
                status=status,
                created_by=users[owner].id,
                location_name=resolution.location_name,
                latitude=resolution.latitude,
                longitude=resolution.longitude,
                location_source=resolution.source,
                location_confidence=resolution.confidence,
            )
            created_disasters += 1
            log.info(
                "disaster_seeded",
                title=title,
                place=resolution.location_name,
                source=resolution.source,
                resolved=resolution.is_resolved,
            )

        await session.commit()
        log.info("seed_complete", disasters=created_disasters, resources=created_resources)

    total_resources = 0
    async with factory() as session:
        total_resources = await resource_repo.count_all(session)

    print("\n" + "=" * 68)
    print("  Seed complete")
    print("=" * 68)
    print(f"  Resources in database : {total_resources}")
    print(f"  Demo incidents        : {len(DISASTERS)}")
    print("\n  Accounts (username / password / role):")
    for username, _display, role, password in USERS:
        print(f"    {username:<13} {password:<16} {role}")
    print("\n  Try:")
    print("    curl -s -X POST localhost:8000/api/v1/auth/login \\")
    print("      -H 'content-type: application/json' \\")
    print(f"      -d '{{\"username\":\"admin\",\"password\":\"{settings.seed_admin_password}\"}}'")
    print("    curl 'localhost:8000/api/v1/disasters?tag=flood'")
    print("    open http://localhost:8000/demo/   (interactive map)")
    print("=" * 68 + "\n")


async def main() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
