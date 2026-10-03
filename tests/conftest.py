import os
from collections.abc import AsyncGenerator

import pytest

# Settings are read at import time, so the environment has to be ready first
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5437/nutrijurnal_test"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation")
os.environ.setdefault("ENVIRONMENT", "local")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

# SQLite keeps the suite dependency-free; swap in Postgres when a test needs it
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        yield session


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield session
        await session.commit()

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client

    app.dependency_overrides.clear()
