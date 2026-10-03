"""A person's data: all of it handed back as one file, and all of it gone
when the account is closed — every table the features added included."""

import base64
import json

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeletedMeal,
    FavouriteFood,
    Food,
    Meal,
    PushSubscription,
    Recipe,
    Reminder,
    User,
    WaterEntry,
    WeightEntry,
)
from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"
FCM = "https://fcm.googleapis.com/fcm/send/account-test"
KEYS = {"p256dh": "BBrowsersPublicKeyStandIn", "auth": "AuthSecretStandIn"}


async def _fill(client: AsyncClient, headers: dict, name: str) -> dict:
    """One of everything a person can put in."""
    food = await client.post(
        f"{PREFIX}/eating/foods", json={"name": f"{name} hleb", "kcal": 250}, headers=headers
    )
    await client.put(f"{PREFIX}/eating/foods/{food.json()['id']}/favourite", headers=headers)
    meal = await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": DAY,
            "slot": "breakfast",
            "title": f"{name} doručak",
            "items": [{"food_id": food.json()["id"], "quantity": 80, "unit": "g"}],
        },
        headers=headers,
    )
    await client.post(
        f"{PREFIX}/eating/meals/{meal.json()['id']}/voice",
        files={"file": ("voice.webm", b"recorded-words", "audio/webm")},
        data={"seconds": "2"},
        headers=headers,
    )
    gone = await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": DAY, "items": [{"label": "Keks", "macros": {"kcal": 90}}]},
        headers=headers,
    )
    await client.delete(f"{PREFIX}/eating/meals/{gone.json()['id']}", headers=headers)
    await client.post(
        f"{PREFIX}/eating/recipes",
        json={"title": f"{name} supa", "stated": {"kcal": 300}},
        headers=headers,
    )
    await client.post(f"{PREFIX}/eating/water", json={"day": DAY, "ml": 500}, headers=headers)
    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 71.3}, headers=headers)
    await client.post(f"{PREFIX}/reminders", json={"kind": "water", "at": "11:00"}, headers=headers)
    await client.post(
        f"{PREFIX}/push/subscriptions",
        json={"endpoint": f"{FCM}-{name}", "keys": KEYS},
        headers=headers,
    )
    return meal.json()


async def test_the_export_holds_everything_and_only_mine(client: AsyncClient):
    mine = await auth_headers(client, "export@example.com", "Ana")
    theirs = await auth_headers(client, "export-other@example.com")
    await _fill(client, mine, "Moj")
    await _fill(client, theirs, "Tuđ")

    response = await client.get(f"{PREFIX}/auth/me/export", headers=mine)

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="Nutrijurnal_export_'
    )
    document = json.loads(response.content)
    assert document["app"] == "Nutrijurnal" and document["format"] == 1
    assert document["account"]["email"] == "export@example.com"
    assert [meal["title"] for meal in document["meals"]] == ["Moj doručak"]
    meal = document["meals"][0]
    assert meal["items"][0]["kcal100"] == 250 and meal["items"][0]["macros"]["kcal"] == 200
    assert meal["voice"] == {"type": "audio/webm", "seconds": 2.0, "transcribed": False}
    assert [food["name"] for food in document["foods"]] == ["Moj hleb"]
    assert document["starred_foods"] == [document["foods"][0]["id"]]
    assert [recipe["title"] for recipe in document["recipes"]] == ["Moj supa"]
    assert [entry["ml"] for entry in document["water"]] == [500]
    assert document["weight"] == [{"day": DAY, "kg": 71.3}]
    assert document["reminders"][0]["kind"] == "water"
    # The browsers subscribed to reminders are not part of it
    assert "fcm.googleapis.com" not in response.text
    assert "Tuđ" not in response.text


async def test_recordings_come_along_only_when_asked(client: AsyncClient):
    headers = await auth_headers(client, "recordings@example.com")
    await _fill(client, headers, "Glas")

    plain = json.loads((await client.get(f"{PREFIX}/auth/me/export", headers=headers)).content)
    full = json.loads(
        (
            await client.get(
                f"{PREFIX}/auth/me/export", params={"recordings": True}, headers=headers
            )
        ).content
    )

    assert "base64" not in plain["meals"][0]["voice"]
    assert base64.b64decode(full["meals"][0]["voice"]["base64"]) == b"recorded-words"


async def test_closing_an_account_takes_every_feature_table_with_it(
    client: AsyncClient, session: AsyncSession
):
    leaving = await auth_headers(client, "leaving@example.com")
    staying = await auth_headers(client, "staying@example.com")
    await _fill(client, leaving, "Odlazim")
    await _fill(client, staying, "Ostajem")

    response = await client.delete(f"{PREFIX}/auth/me", headers=leaving)

    assert response.status_code == 200
    tables = (
        Meal,
        Food,
        Recipe,
        FavouriteFood,
        WaterEntry,
        WeightEntry,
        Reminder,
        PushSubscription,
        DeletedMeal,
    )
    for table in tables:
        # Only the staying person's rows are left (shared foods have no owner)
        owners = set(
            (await session.execute(select(table.user_id).where(table.user_id.is_not(None))))
            .scalars()
            .all()
        )
        assert len(owners) == 1, table.__tablename__
    assert await session.scalar(select(func.count()).select_from(User)) == 1
