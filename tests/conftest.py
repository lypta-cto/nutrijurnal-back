import json
import os
import types
from collections.abc import AsyncGenerator

import pytest

# Settings are read at import time, so the environment has to be ready first
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5437/nutrijurnal_test"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation")
os.environ.setdefault("ENVIRONMENT", "local")
# Settings also read .env, so whatever a developer puts there must not change
# what the suite expects: Google stays off and the refresh cookie travels over
# the plain-http test transport
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GOOGLE_CLIENT_SECRET"] = ""
os.environ["COOKIE_SECURE"] = "false"
os.environ["COOKIE_SAMESITE"] = "lax"
os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "15"
os.environ["REFRESH_TOKEN_EXPIRE_DAYS"] = "30"
# Push stays off unless a test hands the server keys, and the demo keeps its
# shipped limits — a developer's VAPID pair or demo tuning must not leak in
os.environ["VAPID_PUBLIC_KEY"] = ""
os.environ["VAPID_PRIVATE_KEY"] = ""
os.environ["DEMO_ENABLED"] = "true"
os.environ["DEMO_TTL_DAYS"] = "3"
os.environ["DEMO_PER_HOUR"] = "10"
# The sign-in and sign-up throttles keep their shipped limits too, and the
# test transport's address is the client's own: no proxy is trusted
os.environ["LOGIN_PER_ADDRESS"] = "30"
os.environ["LOGIN_FAILURES_PER_EMAIL"] = "10"
os.environ["REGISTER_PER_HOUR"] = "10"
os.environ["TRUSTED_PROXY"] = "false"

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core import database, rate_limit  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import eating_seed, food_lookup  # noqa: E402
from tests.helpers import SMALL_PANTRY  # noqa: E402

# SQLite keeps the suite dependency-free; swap in Postgres when a test needs it
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def pytest_configure(config):
    # SQLAlchemy warns about queries that mean something other than they say
    # (a column-level DISTINCT, a relationship it will not load). The suite
    # treats those as failures, so a query like that never ships quietly.
    config.addinivalue_line("filterwarnings", "error::sqlalchemy.exc.SAWarning")


@pytest.fixture
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})

    # SQLite ignores ON DELETE CASCADE unless asked; Postgres always honours it,
    # and deleting an account relies on it, so the suite must too
    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys_on(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture
def session_maker(engine, monkeypatch) -> async_sessionmaker[AsyncSession]:
    """The app's own SessionLocal, pointed at the test database. Every request
    then runs through the real `get_session` — a fresh session per request,
    committed on success and rolled back on an error — so nothing a test sees
    depends on one long-lived session's identity map or on autoflush that
    production has switched off."""
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=database.SessionLocal.kw["expire_on_commit"],
        autoflush=database.SessionLocal.kw["autoflush"],
    )
    monkeypatch.setattr(database, "SessionLocal", maker)
    return maker


@pytest.fixture
async def session(session_maker) -> AsyncGenerator[AsyncSession, None]:
    """Direct access to the database, for arranging rows and checking them."""
    async with session_maker() as session:
        yield session


@pytest.fixture
async def client(session_maker) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def fresh_throttles():
    """Every request of the suite comes from one address; each test starts
    with none of the earlier ones counted against it."""
    rate_limit.reset()


@pytest.fixture(autouse=True)
def uploads(tmp_path, monkeypatch):
    """Avatars land in a throwaway folder, never in the repo's uploads/."""
    folder = tmp_path / "uploads"
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(folder))
    return folder


class OpenFoodFacts:
    """Stands in for world.openfoodfacts.org, so no test ever reaches the
    network. Put a product under its barcode to have it found; everything
    else answers the way the real API answers an unknown code."""

    def __init__(self) -> None:
        self.products: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.failure: Exception | None = None
        self.status: int | None = None

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if self.status is not None:
            return httpx.Response(self.status, text="Service unavailable")
        barcode = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        product = self.products.get(barcode)
        if product is None:
            return httpx.Response(404, json={"status": 0, "status_verbose": "product not found"})
        return httpx.Response(200, json={"code": barcode, "status": 1, "product": product})


@pytest.fixture(autouse=True)
def open_food_facts(monkeypatch) -> OpenFoodFacts:
    fake = OpenFoodFacts()
    offline = types.ModuleType("httpx")
    offline.__dict__.update(httpx.__dict__)
    offline.AsyncClient = lambda **options: httpx.AsyncClient(
        transport=httpx.MockTransport(fake.answer), **options
    )
    monkeypatch.setattr(food_lookup, "httpx", offline)
    return fake


@pytest.fixture
async def seeds(tmp_path, monkeypatch, session: AsyncSession):
    """A small pantry instead of the shipped one, loaded the way boot loads it."""
    foods = tmp_path / "foods.json"
    foods.write_text(json.dumps(SMALL_PANTRY), encoding="utf-8")
    monkeypatch.setattr(eating_seed, "FOODS_FILE", foods)
    await eating_seed.seed_foods(session)
    await session.commit()
    return foods


@pytest.fixture
async def pantry(session: AsyncSession) -> int:
    """The 180 shared foods that actually ship, for tests about real wording."""
    touched = await eating_seed.seed_foods(session)
    await session.commit()
    return touched
