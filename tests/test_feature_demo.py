"""Try the demo: one tap into a diary with two weeks already in it, kept
apart from everyone else's, deleted when it expires — unless it is kept."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import demo as demo_routes
from app.core.config import settings
from app.models import Meal, User
from app.services import eating_seed, reminders
from tests.helpers import PREFIX, auth_headers

TODAY = date(2026, 9, 21)


@pytest.fixture(autouse=True)
async def shipped(session: AsyncSession) -> None:
    """The real pantry — the demo's plates are built from it — and a fresh
    start for the per-address limit."""
    await eating_seed.seed_foods(session)
    await session.commit()
    demo_routes._started.clear()


async def _demo(client: AsyncClient) -> tuple[dict, dict]:
    response = await client.post(f"{PREFIX}/auth/demo", json={"today": TODAY.isoformat()})
    assert response.status_code == 201, response.text
    body = response.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


async def test_a_demo_is_signed_in_onboarded_and_expiring(client: AsyncClient):
    body, _ = await _demo(client)

    user = body["user"]
    assert user["is_demo"] is True
    assert user["onboarded_at"] is not None
    expires = datetime.fromisoformat(user["demo_expires_at"])
    left = expires.replace(tzinfo=expires.tzinfo or UTC) - datetime.now(UTC)
    assert (
        timedelta(days=settings.DEMO_TTL_DAYS - 1) < left <= timedelta(days=settings.DEMO_TTL_DAYS)
    )
    assert settings.REFRESH_COOKIE_NAME in client.cookies


async def test_a_demo_has_two_weeks_of_living_in_it(client: AsyncClient):
    _, headers = await _demo(client)

    progress = await client.get(
        f"{PREFIX}/eating/progress",
        params={
            "from": (TODAY - timedelta(days=13)).isoformat(),
            "to": TODAY.isoformat(),
            "today": TODAY.isoformat(),
        },
        headers=headers,
    )

    body = progress.json()
    assert body["averages"]["logged_days"] == 14
    assert body["streak"]["current"] == 14
    assert 1100 < body["averages"]["kcal"] < 3200
    assert all(day["water_ml"] > 0 for day in body["days"])
    assert len([day for day in body["days"] if day["weight_kg"]]) == 7
    assert body["weight"]["change"] is not None
    assert body["target_kcal"] and body["target_protein"]

    today = (await client.get(f"{PREFIX}/eating/days/{TODAY}", headers=headers)).json()
    # Today is still going: breakfast and a snack, room to add the rest
    assert sorted(meal["slot"] for meal in today["meals"]) == ["breakfast", "snack"]
    assert all(meal["items"] and meal["kcal"] > 0 for meal in today["meals"])

    recipes = (await client.get(f"{PREFIX}/eating/recipes", headers=headers)).json()
    assert len(recipes) == 2 and all(recipe["kcal"] > 0 for recipe in recipes)
    quick = (await client.get(f"{PREFIX}/eating/foods/quick", headers=headers)).json()
    assert len(quick["favourites"]) == 3
    settings_body = (await client.get(f"{PREFIX}/eating/settings", headers=headers)).json()
    assert settings_body["profile"]["goal"] == "lose"


async def test_every_demo_is_its_own(client: AsyncClient):
    first, first_headers = await _demo(client)
    second, second_headers = await _demo(client)

    assert first["user"]["email"] != second["user"]["email"]
    day = (await client.get(f"{PREFIX}/eating/days/{TODAY}", headers=first_headers)).json()
    meal = day["meals"][0]["id"]
    stranger = await client.patch(
        f"{PREFIX}/eating/meals/{meal}", json={"title": "Not yours"}, headers=second_headers
    )
    assert stranger.status_code == 404


async def test_one_address_cannot_fill_the_database(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_PER_HOUR", 2)

    await _demo(client)
    await _demo(client)
    third = await client.post(f"{PREFIX}/auth/demo", json={})

    assert third.status_code == 429


async def test_the_demo_can_be_switched_off(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_ENABLED", False)

    assert (await client.post(f"{PREFIX}/auth/demo", json={})).status_code == 404


async def test_a_demo_can_be_kept_as_a_real_account(client: AsyncClient):
    _, headers = await _demo(client)

    kept = await client.post(
        f"{PREFIX}/auth/demo/claim",
        json={"email": "Kept@Example.com", "password": "keep-it-123", "full_name": "Ana"},
        headers=headers,
    )

    assert kept.status_code == 200, kept.text
    user = kept.json()
    assert (user["is_demo"], user["demo_expires_at"], user["email"]) == (
        False,
        None,
        "kept@example.com",
    )
    login = await client.post(
        f"{PREFIX}/auth/login", json={"email": "kept@example.com", "password": "keep-it-123"}
    )
    assert login.status_code == 200
    again = await client.post(
        f"{PREFIX}/auth/demo/claim",
        json={"email": "other@example.com", "password": "keep-it-123", "full_name": "Ana"},
        headers=headers,
    )
    assert again.status_code == 400


async def test_a_demo_cannot_take_someone_elses_email(client: AsyncClient):
    await auth_headers(client, "taken@example.com")
    _, headers = await _demo(client)

    response = await client.post(
        f"{PREFIX}/auth/demo/claim",
        json={"email": "taken@example.com", "password": "keep-it-123", "full_name": "Ana"},
        headers=headers,
    )

    assert response.status_code == 409


async def test_expired_demos_are_deleted_with_everything_in_them(
    client: AsyncClient, session: AsyncSession
):
    _, expiring = await _demo(client)
    _, kept = await _demo(client)
    await client.post(
        f"{PREFIX}/auth/demo/claim",
        json={"email": "stays@example.com", "password": "keep-it-123", "full_name": "Ana"},
        headers=kept,
    )
    real = await auth_headers(client, "real@example.com")

    await reminders.tidy(datetime.now(UTC) + timedelta(days=settings.DEMO_TTL_DAYS + 1))

    emails = set((await session.execute(select(User.email))).scalars())
    assert emails == {"stays@example.com", "real@example.com"}
    assert await session.scalar(select(func.count()).select_from(Meal)) > 0
    assert (await client.get(f"{PREFIX}/auth/me", headers=expiring)).status_code == 401
    assert (await client.get(f"{PREFIX}/auth/me", headers=real)).status_code == 200
