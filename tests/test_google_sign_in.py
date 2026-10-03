"""Google is the door for people let in by name: GOOGLE_ALLOWED_EMAILS decides,
before any account is created or linked. Signing up with a password stays
open to anyone."""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import oauth as oauth_route
from app.core.config import settings
from app.models import User
from tests.helpers import PREFIX, sign_up

CALLBACK = f"{PREFIX}/auth/google/callback"


@pytest.fixture
def google(monkeypatch) -> dict:
    """Google switched on, answering with whoever the test says signed in."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example")
    claims = {"sub": "google-1", "email": "ana@gmail.com", "email_verified": True, "name": "Ana"}

    async def authorize_access_token(_request):
        return {"userinfo": dict(claims)}

    monkeypatch.setattr(
        oauth_route,
        "oauth",
        SimpleNamespace(google=SimpleNamespace(authorize_access_token=authorize_access_token)),
    )
    return claims


async def _users_named(session: AsyncSession, email: str) -> int:
    return await session.scalar(select(func.count()).select_from(User).where(User.email == email))


async def test_an_approved_address_gets_an_account_and_a_session(
    client: AsyncClient, session: AsyncSession, google, monkeypatch
):
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_EMAILS", ["ana@gmail.com"])

    response = await client.get(CALLBACK)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "https://app.example/auth/callback"
    assert settings.REFRESH_COOKIE_NAME in response.headers.get("set-cookie", "")
    assert await _users_named(session, "ana@gmail.com") == 1


async def test_an_address_nobody_approved_is_turned_away_without_an_account(
    client: AsyncClient, session: AsyncSession, google, monkeypatch
):
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_EMAILS", ["someone.else@gmail.com"])

    response = await client.get(CALLBACK)

    assert response.headers["location"] == "https://app.example/login?error=google_not_allowed"
    assert "set-cookie" not in response.headers
    assert await _users_named(session, "ana@gmail.com") == 0


async def test_an_existing_password_account_is_not_linked_unless_approved(
    client: AsyncClient, session: AsyncSession, google, monkeypatch
):
    """Approval is about Google, not about having an account: a person who
    signed up with a password keeps signing in that way."""
    await sign_up(client, "ana@gmail.com")
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_EMAILS", [])

    response = await client.get(CALLBACK)

    assert response.headers["location"].endswith("error=google_not_allowed")
    user = await session.scalar(select(User).where(User.email == "ana@gmail.com"))
    assert user.google_sub is None


async def test_a_whole_domain_can_be_approved(
    client: AsyncClient, session: AsyncSession, google, monkeypatch
):
    google["email"] = "Marko@Lypta.ai"
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_EMAILS", ["@lypta.ai"])

    response = await client.get(CALLBACK)

    assert response.headers["location"] == "https://app.example/auth/callback"
    assert await _users_named(session, "marko@lypta.ai") == 1


def test_the_list_reads_the_way_it_is_typed():
    from app.core.config import Settings

    typed = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5437/nutrijurnal",
        SECRET_KEY="x" * 40,
        GOOGLE_ALLOWED_EMAILS=" Ana@Gmail.com , @lypta.ai ,, ",
        _env_file=None,
    )

    assert typed.GOOGLE_ALLOWED_EMAILS == ["ana@gmail.com", "@lypta.ai"]
    assert typed.google_email_allowed("ANA@gmail.com")
    assert typed.google_email_allowed("luka@lypta.ai")
    # A domain is a whole domain, not a suffix of someone else's
    assert not typed.google_email_allowed("mallory@evil-lypta.ai")
    assert not typed.google_email_allowed("bob@gmail.com")
