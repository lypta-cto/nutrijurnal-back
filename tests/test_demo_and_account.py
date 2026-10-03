"""The edges of the demo and of a person's own data: keeping a demo safely,
the per-address limit over time, a demo started on an odd clock, files left
on disk, and the export of an account with a calculator behind it."""

import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import demo as demo_routes
from app.core.config import settings
from app.services import eating_seed, reminders
from tests.helpers import PASSWORD, PREFIX, auth_headers, bearer

TODAY = date(2026, 9, 21)
KEEP = {"email": "kept@example.com", "password": "keep-it-123", "full_name": "Ana"}


@pytest.fixture(autouse=True)
async def shipped(session: AsyncSession) -> None:
    """The demo builds its plates from the real pantry, and every test starts
    with the per-address limit untouched."""
    await eating_seed.seed_foods(session)
    await session.commit()
    demo_routes._started.clear()


async def demo(client: AsyncClient, today: date = TODAY) -> dict:
    response = await client.post(f"{PREFIX}/auth/demo", json={"today": today.isoformat()})
    assert response.status_code == 201, response.text
    return bearer(response.json()["access_token"])


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (90, 140, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


# --- Keeping a demo -----------------------------------------------------------------


async def test_keeping_a_demo_needs_the_demo_signed_in(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/demo/claim", json=KEEP)

    assert response.status_code == 401


async def test_a_demo_can_not_take_an_email_that_differs_only_in_case(client: AsyncClient):
    await auth_headers(client, "taken@example.com")
    headers = await demo(client)

    response = await client.post(
        f"{PREFIX}/auth/demo/claim", json={**KEEP, "email": "Taken@Example.COM"}, headers=headers
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {**KEEP, "password": "short"},
        {**KEEP, "full_name": "   "},
        {**KEEP, "email": "not-an-email"},
        {"email": KEEP["email"], "password": KEEP["password"]},
    ],
)
async def test_keeping_a_demo_asks_for_a_real_account(client: AsyncClient, body):
    headers = await demo(client)

    response = await client.post(f"{PREFIX}/auth/demo/claim", json=body, headers=headers)

    assert response.status_code == 422


async def test_an_ordinary_account_is_not_a_demo_to_keep(client: AsyncClient):
    headers = await auth_headers(client, "real-one@example.com")

    response = await client.post(f"{PREFIX}/auth/demo/claim", json=KEEP, headers=headers)

    assert response.status_code == 400


async def test_a_kept_demo_is_an_ordinary_account_from_then_on(client: AsyncClient):
    headers = await demo(client)
    await client.post(f"{PREFIX}/auth/demo/claim", json=KEEP, headers=headers)

    changed = await client.post(
        f"{PREFIX}/auth/me/password",
        json={"current_password": KEEP["password"], "new_password": PASSWORD},
        headers=headers,
    )
    login = await client.post(
        f"{PREFIX}/auth/login", json={"email": KEEP["email"], "password": PASSWORD}
    )

    assert changed.status_code == 200, changed.text
    assert login.status_code == 200
    assert login.json()["user"]["is_demo"] is False
    # The two weeks it came with are still there
    day = (await client.get(f"{PREFIX}/eating/days/{TODAY}", headers=headers)).json()
    assert day["meals"]


async def test_a_demo_can_be_closed_at_once(client: AsyncClient):
    headers = await demo(client)

    assert (await client.delete(f"{PREFIX}/auth/me", headers=headers)).status_code == 200
    assert (await client.get(f"{PREFIX}/auth/me", headers=headers)).status_code == 401


# --- The per-address limit over time -------------------------------------------------------


async def test_the_limit_on_demos_lifts_after_an_hour(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_PER_HOUR", 1)
    clock = {"now": 1000.0}
    monkeypatch.setattr(demo_routes.clock, "monotonic", lambda: clock["now"])

    await demo(client)
    clock["now"] += 3599
    assert (await client.post(f"{PREFIX}/auth/demo", json={})).status_code == 429

    clock["now"] += 2
    assert (await client.post(f"{PREFIX}/auth/demo", json={})).status_code == 201


async def test_addresses_quiet_for_an_hour_are_forgotten(client: AsyncClient, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(demo_routes.clock, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(demo_routes, "SWEEP_AT", 3)
    demo_routes._started.clear()
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        demo_routes._started[address].append(clock["now"])

    clock["now"] += 3601
    assert (await client.post(f"{PREFIX}/auth/demo", json={})).status_code == 201

    assert set(demo_routes._started) == {"127.0.0.1"}


async def test_a_refused_demo_does_not_count_against_the_hour(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_PER_HOUR", 1)
    clock = {"now": 1000.0}
    monkeypatch.setattr(demo_routes.clock, "monotonic", lambda: clock["now"])

    await demo(client)
    for _ in range(5):
        clock["now"] += 600
        await client.post(f"{PREFIX}/auth/demo", json={})

    # An hour after the one that went through, not after the last refusal
    clock["now"] = 1000.0 + 3601
    assert (await client.post(f"{PREFIX}/auth/demo", json={})).status_code == 201


# --- A demo started on an odd clock ------------------------------------------------------


async def test_a_demo_without_a_today_ends_on_the_servers(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/demo", json={})

    assert response.status_code == 201
    headers = bearer(response.json()["access_token"])
    day = (await client.get(f"{PREFIX}/eating/days/{date.today()}", headers=headers)).json()
    assert day["meals"]


@pytest.mark.parametrize("today", [date(2090, 1, 1), date(1910, 1, 1)])
async def test_a_demo_asked_for_with_a_clock_far_off_ends_on_the_servers_today(
    client: AsyncClient, today
):
    response = await client.post(f"{PREFIX}/auth/demo", json={"today": today.isoformat()})

    assert response.status_code == 201
    headers = bearer(response.json()["access_token"])
    day = (await client.get(f"{PREFIX}/eating/days/{date.today()}", headers=headers)).json()
    assert day["meals"]


async def test_a_demo_asked_for_a_day_ahead_keeps_the_viewers_today(client: AsyncClient):
    """East of UTC it is already tomorrow — that is the viewer's today, not a bad clock."""
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)

    response = await client.post(f"{PREFIX}/auth/demo", json={"today": tomorrow.isoformat()})

    headers = bearer(response.json()["access_token"])
    day = (await client.get(f"{PREFIX}/eating/days/{tomorrow}", headers=headers)).json()
    assert day["meals"]


# --- Files left on disk ---------------------------------------------------------------------


async def test_an_expired_demo_takes_its_avatar_with_it(client: AsyncClient, uploads):
    headers = await demo(client)
    await client.post(
        f"{PREFIX}/auth/me/avatar", files={"file": ("me.png", png(), "image/png")}, headers=headers
    )
    assert list((uploads / "avatars").iterdir())

    await reminders.tidy(datetime.now(UTC) + timedelta(days=settings.DEMO_TTL_DAYS + 1))

    assert list((uploads / "avatars").iterdir()) == []


async def test_a_kept_demo_keeps_its_avatar_past_the_expiry(client: AsyncClient, uploads):
    headers = await demo(client)
    await client.post(
        f"{PREFIX}/auth/me/avatar", files={"file": ("me.png", png(), "image/png")}, headers=headers
    )
    await client.post(f"{PREFIX}/auth/demo/claim", json=KEEP, headers=headers)

    await reminders.tidy(datetime.now(UTC) + timedelta(days=settings.DEMO_TTL_DAYS + 1))

    assert len(list((uploads / "avatars").iterdir())) == 1
    assert (await client.get(f"{PREFIX}/auth/me", headers=headers)).status_code == 200


# --- The export of an account with a calculator behind it -----------------------------------


async def test_the_export_carries_the_targets_and_the_answers_behind_them(client: AsyncClient):
    headers = await auth_headers(client, "planned@example.com")
    profile = {
        "sex": "male",
        "birth_year": 1991,
        "height_cm": 180,
        "weight_kg": 80,
        "activity": "moderate",
        "goal": "lose",
        "pace": 0.5,
        "protein_per_kg": None,
        "fat_percent": 30,
    }
    await client.patch(
        f"{PREFIX}/eating/settings",
        json={
            "target_kcal": 2170,
            "target_protein": 144,
            "profile": profile,
            "onboarded": True,
            "timezone": "Europe/Belgrade",
            "water_goal_ml": 2500,
        },
        headers=headers,
    )

    document = json.loads((await client.get(f"{PREFIX}/auth/me/export", headers=headers)).content)

    account = document["account"]
    assert (account["target_kcal"], account["target_protein"], account["target_carbs"]) == (
        2170,
        144,
        None,
    )
    assert account["goal_profile"] == profile
    assert (account["timezone"], account["water_goal_ml"]) == ("Europe/Belgrade", 2500)
    assert account["onboarded_at"] is not None
    # Nothing that would let someone sign in as the person
    assert "hashed_password" not in json.dumps(document)
    assert "password" not in account


async def test_the_export_holds_a_restored_meal_once_and_a_deleted_one_not_at_all(
    client: AsyncClient,
):
    headers = await auth_headers(client, "undo-export@example.com")
    kept = await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": "2026-09-21", "title": "Vraćen", "items": []},
        headers=headers,
    )
    gone = await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": "2026-09-21", "title": "Obrisan", "items": []},
        headers=headers,
    )
    await client.delete(f"{PREFIX}/eating/meals/{kept.json()['id']}", headers=headers)
    await client.post(f"{PREFIX}/eating/meals/{kept.json()['id']}/restore", headers=headers)
    await client.delete(f"{PREFIX}/eating/meals/{gone.json()['id']}", headers=headers)

    document = json.loads((await client.get(f"{PREFIX}/auth/me/export", headers=headers)).content)

    assert [meal["title"] for meal in document["meals"]] == ["Vraćen"]


async def test_the_export_needs_a_session(client: AsyncClient):
    assert (await client.get(f"{PREFIX}/auth/me/export")).status_code == 401
