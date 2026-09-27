"""Test fixtures.

Two isolation problems have to be solved for these tests to be trustworthy:

**Database.** The suite runs against a real PostGIS database (``disaster_response_test``),
because the whole point of several tests is that ``ST_DWithin`` behaves correctly
- mocking the database would test the mock. Migrations run once per session, and
each test runs inside a transaction that is rolled back afterwards, so tests
cannot see one another's writes and order never matters.

**Process-global state.** Caches, circuit breakers and the cache backend are
module-level singletons, which is right for a server and dangerous for a test
suite. ``reset_global_state`` clears all of them between tests; without it, a
test that trips a breaker would fail the next test that expects a working
provider.

External HTTP is never called for real: ``httpx.MockTransport`` intercepts it,
so the suite is fast, offline and deterministic.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable

import pytest
import pytest_asyncio

# Must be set before app.core.config is imported, since settings are read once.
#
# Assignment, NOT setdefault. Under docker compose the container already has
# APP_ENV=development from `.env`, so setdefault would silently do nothing and
# the suite would run against the DEVELOPMENT database - destroying real data and
# producing confusing connection-pool errors. A test harness must pin its own
# environment rather than defer to whatever happens to be set.
os.environ["APP_ENV"] = "test"                # selects TEST_DATABASE_URL
os.environ["RATE_LIMIT_ENABLED"] = "false"    # a shared limiter would fail tests at random
os.environ["GEOCODER_ENABLED"] = "false"      # offline gazetteer only: no network in tests
os.environ["ANTHROPIC_API_KEY"] = ""          # never call an LLM from the suite
os.environ["CACHE_L1_TTL"] = "0"              # disable L1 so cache assertions observe L2

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.cache import cache  # noqa: E402
from app.cache.backend import MemoryBackend, set_backend  # noqa: E402
from app.core.deps import get_session  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.models.enums import ResourceType, UserRole  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.db.session import dispose_engine, get_engine  # noqa: E402
from app.integrations import http as http_module  # noqa: E402
from app.integrations.resilience import reset_all_breakers  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories import resource_repo  # noqa: E402

# Note: there is deliberately no custom `event_loop` fixture here - pytest-asyncio
# removed that override point in 1.0. Loop scoping is configured instead through
# `asyncio_default_fixture_loop_scope = "function"` in pyproject.toml, so every
# per-test fixture shares the test's own loop, plus an explicit `loop_scope` on
# the one session-scoped fixture below.


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _create_schema() -> AsyncGenerator[None, None]:
    """Build the schema once for the whole session.

    ``create_all`` from model metadata rather than ``alembic upgrade head``: it
    is faster and this fixture's job is to test application behaviour. The
    migration itself is exercised by actually running it against the dev
    database (``docker compose exec api alembic upgrade head``).
    """
    from sqlalchemy import text

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Dispose and clear the module-level engine before handing over to the tests.
    # An asyncpg pool is bound to the event loop that created it, and this
    # fixture's loop is not necessarily the one a given test runs in. Leaving the
    # engine in place produces "got Future attached to a different loop".
    await dispose_engine()

    yield

    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await dispose_engine()


@pytest.fixture(autouse=True)
def reset_global_state():
    """Clear process-global state around every test.

    Without this the suite is order-dependent: a tripped circuit breaker or a
    warm cache entry from one test silently changes the behaviour of the next.
    """
    set_backend(MemoryBackend())
    cache.clear_l1()
    reset_all_breakers()
    yield
    set_backend(None)
    cache.clear_l1()
    reset_all_breakers()
    http_module.set_http_client(None)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A session bound to a transaction that is always rolled back.

    The session joins an outer transaction on a single connection, so anything
    the code under test commits is committed only to that transaction - and the
    rollback at the end discards it. This is what makes tests independent
    without truncating tables between them.

    ``join_transaction_mode="create_savepoint"`` is the part that makes this
    work. The service layer really does call ``session.commit()``; without this
    the session would try to start its own transaction on a connection that
    already has one ("cannot use Connection.transaction() in a manually started
    transaction"). With it, each commit releases a SAVEPOINT instead, so the
    application code is genuinely exercised while the outer transaction stays
    open and can still be rolled back.

    The engine is created and disposed inside this fixture so that its asyncpg
    pool always belongs to the event loop the test is running on.
    """
    await dispose_engine()
    engine = get_engine()
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    db_session = factory()

    try:
        yield db_session
    finally:
        await db_session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await dispose_engine()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired to the app, sharing the test's transaction.

    ``get_session`` is overridden so the request handlers use the same
    transaction-bound session as the test, which is what lets a test set up data
    directly and then see it through the API.

    ASGITransport calls the app in-process: no socket, no port, no server to
    start, and tracebacks surface directly in the test.
    """
    app = create_app()

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = _override

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=30.0,
    ) as http_client:
        yield http_client

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Users and authentication
# --------------------------------------------------------------------------- #

_PASSWORD = "test-password-123"


async def _make_user(session: AsyncSession, username: str, role: UserRole) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        password_hash=hash_password(_PASSWORD),
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def admin_user(session: AsyncSession) -> User:
    return await _make_user(session, "test_admin", UserRole.ADMIN)


@pytest.fixture
async def contributor_user(session: AsyncSession) -> User:
    return await _make_user(session, "test_contributor", UserRole.CONTRIBUTOR)


@pytest.fixture
async def other_contributor_user(session: AsyncSession) -> User:
    return await _make_user(session, "test_contributor_two", UserRole.CONTRIBUTOR)


@pytest.fixture
async def responder_user(session: AsyncSession) -> User:
    return await _make_user(session, "test_responder", UserRole.RESPONDER)


def auth_header(user: User) -> dict[str, str]:
    """Mint a valid token for ``user``.

    The token is produced by the real ``create_access_token``, so the tests
    exercise genuine signing and verification rather than a bypass.
    """
    from app.core.security import create_access_token

    token, _ = create_access_token(
        subject=str(user.id), username=user.username, role=str(user.role)
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(admin_user: User) -> dict[str, str]:
    return auth_header(admin_user)


@pytest.fixture
def contributor_headers(contributor_user: User) -> dict[str, str]:
    return auth_header(contributor_user)


@pytest.fixture
def other_contributor_headers(other_contributor_user: User) -> dict[str, str]:
    return auth_header(other_contributor_user)


@pytest.fixture
def responder_headers(responder_user: User) -> dict[str, str]:
    return auth_header(responder_user)


# --------------------------------------------------------------------------- #
# Geospatial fixture data
# --------------------------------------------------------------------------- #

# Reference point: Lower Manhattan. Every seeded resource below has a known
# approximate distance from it, so radius assertions are exact rather than
# "whatever the data happened to be".
NYC_LAT, NYC_LNG = 40.7128, -74.0060


@pytest.fixture
async def geo_resources(session: AsyncSession) -> dict[str, object]:
    """Resources at known distances from the NYC reference point.

    Approximate distances:
        very_close  ~0.1 km
        close       ~1.2 km
        mid         ~4.5 km
        far         ~8.5 km
        very_far    ~5,500 km  (London - always outside any sane radius)
    """
    made = {}
    points = [
        ("very_close", ResourceType.WATER, 40.7130, -74.0058),
        ("close", ResourceType.SHELTER, 40.7230, -74.0060),
        ("mid", ResourceType.HOSPITAL, 40.7528, -74.0060),
        ("far", ResourceType.FOOD, 40.7893, -74.0060),
        ("very_far", ResourceType.RESCUE, 51.5074, -0.1278),
    ]
    for name, resource_type, lat, lng in points:
        made[name] = await resource_repo.create(
            session,
            name=f"geo_{name}",
            resource_type=resource_type,
            latitude=lat,
            longitude=lng,
        )
    await session.flush()
    return made


# --------------------------------------------------------------------------- #
# External HTTP mocking
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_transport() -> Callable[..., httpx.MockTransport]:
    """Build a MockTransport for the external providers.

    Returns a factory so each test chooses its own scenario (success, failure,
    slow) and can count how many times the origin was actually called - which is
    how the cache tests prove a hit avoided the network.
    """

    def _build(
        *,
        social_status: int = 200,
        official_status: int = 200,
        social_posts: int = 3,
        counter: dict[str, int] | None = None,
    ) -> httpx.MockTransport:
        calls = counter if counter is not None else {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls[path] = calls.get(path, 0) + 1

            if "/social/search" in path:
                if social_status != 200:
                    return httpx.Response(social_status, json={"detail": "upstream down"})
                return httpx.Response(
                    200,
                    json={
                        "query": "test",
                        "result_count": social_posts,
                        "posts": [
                            {
                                "post_id": f"sp_{i}",
                                "content": (
                                    "URGENT: family trapped on the roof, water still rising"
                                    if i == 0
                                    else f"Need drinking water near the shelter, report {i}"
                                ),
                                "user": f"citizen{i}",
                                "created_at": "2026-09-27T09:14:00+00:00",
                                "likes": i * 10,
                                "verified_account": False,
                            }
                            for i in range(social_posts)
                        ],
                    },
                )

            if "/official/bulletins" in path:
                if official_status != 200:
                    return httpx.Response(official_status, json={"detail": "upstream down"})
                return httpx.Response(
                    200,
                    json={
                        "region": "test",
                        "count": 1,
                        "bulletins": [
                            {
                                "ref": "BUL-TEST01",
                                "headline": "Evacuation advisory issued",
                                "body": "Residents should move to relief centres.",
                                "issued_at": "2026-09-27T08:00:00+00:00",
                                "agency": "Test Emergency Authority",
                                "severity": "high",
                            }
                        ],
                    },
                )

            return httpx.Response(404, json={"detail": "not mocked"})

        return httpx.MockTransport(handler)

    return _build


@pytest.fixture
def install_mock_http(mock_transport):
    """Install a mocked httpx client for the duration of a test."""

    def _install(**kwargs) -> dict[str, int]:
        counter: dict[str, int] = {}
        transport = mock_transport(counter=counter, **kwargs)
        http_module.set_http_client(httpx.AsyncClient(transport=transport))
        return counter

    return _install
