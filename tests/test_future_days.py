"""Nothing is written onto a day that has not come yet: Today never opens
one, so a meal put there would simply vanish from the diary."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers, meal

UTC_TODAY = datetime.now(UTC).date()
# Somewhere east of UTC it is already tomorrow — that day has come for someone
TOMORROW = (UTC_TODAY + timedelta(days=1)).isoformat()
TO_COME = (UTC_TODAY + timedelta(days=2)).isoformat()
TODAY = UTC_TODAY.isoformat()


@pytest.fixture
async def me(client: AsyncClient) -> dict:
    return await auth_headers(client, "ahead@example.com")


def _refused_for_the_day(response) -> None:
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"][-1] == "day"
    assert error["msg"] == "That day hasn't come yet — pick today or an earlier day"


async def test_a_meal_goes_on_a_day_that_has_come_somewhere(client: AsyncClient, me):
    for day in (TODAY, TOMORROW):
        assert (await meal(client, me, day))["day"] == day

    response = await client.post(
        f"{PREFIX}/eating/meals", json={"day": TO_COME, "title": "Later"}, headers=me
    )
    _refused_for_the_day(response)
    later = (await client.get(f"{PREFIX}/eating/days/{TO_COME}", headers=me)).json()
    assert later["meals"] == []


async def test_a_meal_is_not_moved_to_a_day_to_come(client: AsyncClient, me):
    saved = await meal(client, me, TODAY)

    moved = await client.patch(
        f"{PREFIX}/eating/meals/{saved['id']}", json={"day": TO_COME}, headers=me
    )

    _refused_for_the_day(moved)
    day = (await client.get(f"{PREFIX}/eating/days/{TODAY}", headers=me)).json()
    assert [entry["id"] for entry in day["meals"]] == [saved["id"]]


async def test_no_copy_recipe_water_or_weight_lands_on_a_day_to_come(client: AsyncClient, me):
    saved = await meal(client, me, TODAY)
    recipe = await client.post(
        f"{PREFIX}/eating/recipes",
        json={"title": "Supa", "stated": {"kcal": 300}},
        headers=me,
    )

    for response in (
        await client.post(
            f"{PREFIX}/eating/meals/{saved['id']}/copy", json={"day": TO_COME}, headers=me
        ),
        await client.post(
            f"{PREFIX}/eating/days/{TO_COME}/copy", json={"from_day": TODAY}, headers=me
        ),
        await client.post(
            f"{PREFIX}/eating/meals/from-recipe",
            json={"recipe_id": recipe.json()["id"], "day": TO_COME},
            headers=me,
        ),
        await client.post(f"{PREFIX}/eating/water", json={"day": TO_COME, "ml": 250}, headers=me),
        await client.put(f"{PREFIX}/eating/weight/{TO_COME}", json={"kg": 80}, headers=me),
    ):
        _refused_for_the_day(response)

    # The same writes onto a day that has come still go through
    copied = await client.post(
        f"{PREFIX}/eating/days/{TOMORROW}/copy", json={"from_day": TODAY}, headers=me
    )
    weighed = await client.put(f"{PREFIX}/eating/weight/{TOMORROW}", json={"kg": 80}, headers=me)
    assert (copied.status_code, weighed.status_code) == (201, 200)
