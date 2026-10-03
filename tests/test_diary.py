"""Writing the diary: meals and their items, what a day and a week add up to,
and the rule that a meal keeps the numbers it was eaten with."""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MealItem
from app.services import eating_seed
from tests.helpers import PREFIX, SMALL_PANTRY, auth_headers, find_food, meal, own_food

DAY = "2026-09-21"


@pytest.fixture
async def me(client: AsyncClient, seeds) -> dict:
    return await auth_headers(client, email="diary@example.com")


async def _day(client: AsyncClient, headers: dict, day: str = DAY) -> dict:
    response = await client.get(f"{PREFIX}/eating/days/{day}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- Meals --------------------------------------------------------------------


async def test_a_meal_needs_only_a_day(client: AsyncClient, me):
    response = await client.post(f"{PREFIX}/eating/meals", json={"day": DAY}, headers=me)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] and body["day"] == DAY
    assert (body["at"], body["note"], body["items"]) == (None, None, [])
    assert body["kcal"] == 0 and body["has_voice"] is False and body["recipe_id"] is None


async def test_what_a_meal_accepts_is_bounded(client: AsyncClient, me):
    item = {"label": "x", "quantity": 1}
    cases = {
        "no day": {"title": "x"},
        "not a day": {"day": "2026-02-30"},
        "long title": {"day": DAY, "title": "x" * 161},
        "long note": {"day": DAY, "note": "x" * 4001},
        "not a time": {"day": DAY, "at": "25:00"},
        "101 items": {"day": DAY, "items": [item] * 101},
        "negative amount": {"day": DAY, "items": [{**item, "quantity": -1}]},
        "absurd amount": {"day": DAY, "items": [{**item, "quantity": 10001}]},
    }
    for case, body in cases.items():
        response = await client.post(f"{PREFIX}/eating/meals", json=body, headers=me)
        assert response.status_code == 422, case

    # Right at the limits is still fine
    edge = await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": DAY, "title": "x" * 160, "items": [{**item, "quantity": 10000}] * 100},
        headers=me,
    )
    assert edge.status_code == 201


async def test_a_meal_can_be_renamed_moved_and_retimed(client: AsyncClient, me):
    saved = await meal(client, me, DAY, title="Doručak", at="08:00:00", note="kafa")
    path = f"{PREFIX}/eating/meals/{saved['id']}"

    renamed = await client.patch(path, json={"title": "Brunch", "at": "11:30:00"}, headers=me)
    assert renamed.status_code == 200
    assert (renamed.json()["title"], renamed.json()["at"]) == ("Brunch", "11:30:00")

    # Nulls clear what may be empty and leave the title alone
    cleared = await client.patch(path, json={"at": None, "note": None, "title": None}, headers=me)
    assert (cleared.json()["title"], cleared.json()["at"], cleared.json()["note"]) == (
        "Brunch",
        None,
        None,
    )
    assert (await client.patch(path, json={"title": ""}, headers=me)).status_code == 422

    moved = await client.patch(path, json={"day": "2026-09-20"}, headers=me)
    assert moved.json()["day"] == "2026-09-20"
    assert (await _day(client, me))["meals"] == []
    assert [m["title"] for m in (await _day(client, me, "2026-09-20"))["meals"]] == ["Brunch"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG app/api/routes/eating.py update_meal(): MealPatch allows day=null and the handler "
        "setattr()s it onto the NOT NULL column; the IntegrityError is caught by main.py's "
        "DBAPIError handler and answered as 503 'Database unavailable. Is Postgres running?' "
        "instead of a 422 (or leaving the day as it was)"
    ),
)
async def test_a_meal_can_not_be_moved_to_no_day(client: AsyncClient, me):
    saved = await meal(client, me, DAY)

    response = await client.patch(
        f"{PREFIX}/eating/meals/{saved['id']}", json={"day": None}, headers=me
    )

    assert response.status_code in (200, 422), response.text
    assert [m["id"] for m in (await _day(client, me))["meals"]] == [saved["id"]]


async def test_deleting_a_meal_takes_its_items_with_it(
    client: AsyncClient, me, session: AsyncSession
):
    banana = await find_food(client, me, "banana", "Banana")
    saved = await meal(
        client,
        me,
        DAY,
        {"food_id": banana["id"], "quantity": 1, "unit": "piece"},
        {"label": "Cake", "macros": {"kcal": 300}},
    )
    path = f"{PREFIX}/eating/meals/{saved['id']}"

    assert (await client.delete(path, headers=me)).status_code == 204
    assert (await _day(client, me))["meals"] == []
    assert await session.scalar(select(func.count()).select_from(MealItem)) == 0
    # Gone means gone: a second delete has nothing to find
    assert (await client.delete(path, headers=me)).status_code == 404


# --- Items --------------------------------------------------------------------


async def test_an_item_is_counted_in_its_own_unit(client: AsyncClient, me):
    egg = await find_food(client, me, "jaje", "Jaje")
    oats = await find_food(client, me, "ovsene", "Ovsene pahuljice")
    saved = await meal(
        client,
        me,
        DAY,
        {"food_id": egg["id"], "quantity": 2, "unit": "piece"},
        {"food_id": oats["id"], "quantity": 3, "unit": "tbsp"},
        # Not a unit the food knows: the diary's default for a spoon
        {"food_id": oats["id"], "quantity": 1, "unit": "cup"},
        # Not a unit at all: read as grams
        {"food_id": oats["id"], "quantity": 40, "unit": "bucket"},
    )

    assert [(i["unit"], i["grams"]) for i in saved["items"]] == [
        ("piece", 110),
        ("tbsp", 30),
        ("cup", 240),
        ("g", 40),
    ]
    # Each line carries the food's numbers at that weight
    assert [i["kcal"] for i in saved["items"]] == [170.5, 113.7, 909.6, 151.6]
    assert saved["kcal"] == round(170.5 + 113.7 + 909.6 + 151.6, 1)


async def test_items_are_added_changed_and_removed(client: AsyncClient, me):
    egg = await find_food(client, me, "jaje", "Jaje")
    banana = await find_food(client, me, "banana", "Banana")
    saved = await meal(client, me, DAY, {"food_id": egg["id"], "quantity": 1, "unit": "piece"})
    path = f"{PREFIX}/eating/meals/{saved['id']}/items"

    added = (
        await client.post(
            path, json={"food_id": banana["id"], "quantity": 1, "unit": "piece"}, headers=me
        )
    ).json()
    assert [i["label"] for i in added["items"]] == ["Jaje", "Banana"]
    assert added["kcal"] == round(85.2 + 106.8, 1)
    egg_item, banana_item = added["items"]

    # Two eggs instead of one: the grams follow the food's own piece
    more = (await client.patch(f"{path}/{egg_item['id']}", json={"quantity": 2}, headers=me)).json()
    assert more["items"][0]["grams"] == 110 and more["items"][0]["kcal"] == 170.5

    # Weighed instead of counted
    weighed = (
        await client.patch(
            f"{path}/{egg_item['id']}", json={"unit": "g", "quantity": 60}, headers=me
        )
    ).json()
    assert (weighed["items"][0]["unit"], weighed["items"][0]["grams"]) == ("g", 60)

    # A unit nobody knows is ignored rather than guessed at
    kept = (
        await client.patch(f"{path}/{egg_item['id']}", json={"unit": "bowl"}, headers=me)
    ).json()
    assert kept["items"][0]["unit"] == "g"

    # It was a banana after all: the name and the numbers both follow
    swapped = (
        await client.patch(
            f"{path}/{egg_item['id']}",
            json={"food_id": banana["id"], "quantity": 100, "unit": "g"},
            headers=me,
        )
    ).json()
    assert (swapped["items"][0]["label"], swapped["items"][0]["kcal"]) == ("Banana", 89)

    relabelled = (
        await client.patch(f"{path}/{egg_item['id']}", json={"label": "Mala banana"}, headers=me)
    ).json()
    assert relabelled["items"][0]["label"] == "Mala banana"

    removed = (await client.delete(f"{path}/{banana_item['id']}", headers=me)).json()
    assert [i["label"] for i in removed["items"]] == ["Mala banana"]
    assert removed["kcal"] == 89
    assert (await client.delete(f"{path}/{banana_item['id']}", headers=me)).status_code == 404


async def test_an_unknown_food_id_is_a_404_when_swapping_and_a_blank_line_when_adding(
    client: AsyncClient, me
):
    saved = await meal(client, me, DAY, {"label": "Nešto", "quantity": 100})
    nobody = "00000000-0000-4000-8000-000000000000"

    swapped = await client.patch(
        f"{PREFIX}/eating/meals/{saved['id']}/items/{saved['items'][0]['id']}",
        json={"food_id": nobody},
        headers=me,
    )
    assert swapped.status_code == 404

    added = await client.post(
        f"{PREFIX}/eating/meals/{saved['id']}/items", json={"food_id": nobody}, headers=me
    )
    assert added.status_code == 200
    assert added.json()["items"][-1]["food_id"] is None and added.json()["kcal"] == 0


async def test_quick_kcal_counts_servings_and_a_real_food_wins_over_typed_numbers(
    client: AsyncClient, me
):
    banana = await find_food(client, me, "banana", "Banana")
    saved = await meal(
        client,
        me,
        DAY,
        {"label": "Pizza", "quantity": 2, "macros": {"kcal": 285, "protein": 12, "fat": 10}},
        {"quantity": 1, "macros": {"kcal": 100}},
        # A food was picked, so the numbers typed beside it are ignored
        {"food_id": banana["id"], "quantity": 100, "unit": "g", "macros": {"kcal": 9999}},
    )
    pizza, nameless, fruit = saved["items"]

    assert (pizza["unit"], pizza["kcal"], pizza["protein"], pizza["carbs"]) == (
        "serving",
        570,
        24,
        0,
    )
    assert nameless["label"] == "Quick add" and nameless["kcal"] == 100
    assert fruit["food_id"] == banana["id"] and fruit["kcal"] == 89


# --- Days and weeks -----------------------------------------------------------


async def test_a_day_adds_up_its_meals_in_the_order_they_were_eaten(client: AsyncClient, me):
    banana = await find_food(client, me, "banana", "Banana")
    egg = await find_food(client, me, "jaje", "Jaje")
    await meal(client, me, DAY, {"food_id": banana["id"], "quantity": 100}, title="Untimed")
    await meal(
        client, me, DAY, {"food_id": egg["id"], "quantity": 100}, title="Lunch", at="13:00:00"
    )
    await meal(
        client,
        me,
        DAY,
        {"food_id": banana["id"], "quantity": 200},
        title="Breakfast",
        at="08:00:00",
    )
    await meal(client, me, "2026-09-22", {"food_id": egg["id"], "quantity": 999}, title="Tomorrow")

    day = await _day(client, me)

    # Timed meals by the clock, then anything nobody timed
    assert [m["title"] for m in day["meals"]] == ["Breakfast", "Lunch", "Untimed"]
    assert day["totals"] == {"kcal": 178 + 155 + 89, "protein": 16.3, "carbs": 69.5, "fat": 11.9}
    assert day["target"] is None and day["day"] == DAY


async def test_a_week_lists_only_the_days_something_was_eaten(client: AsyncClient, me):
    banana = await find_food(client, me, "banana", "Banana")
    for day, grams in (("2026-09-22", 100), ("2026-09-24", 100), ("2026-09-24", 200)):
        await meal(client, me, day, {"food_id": banana["id"], "quantity": grams})
    await meal(client, me, "2026-09-24", title="Water only")
    # Outside the week on both sides
    await meal(client, me, "2026-09-20", {"food_id": banana["id"], "quantity": 100})
    await meal(client, me, "2026-09-29", {"food_id": banana["id"], "quantity": 100})

    week = (
        await client.get(
            f"{PREFIX}/eating/days", params={"from": "2026-09-21", "to": "2026-09-27"}, headers=me
        )
    ).json()

    assert [(d["day"], d["meals"], d["kcal"]) for d in week] == [
        ("2026-09-22", 1, 89),
        ("2026-09-24", 3, 267),
    ]
    # One day is a range too
    single = await client.get(
        f"{PREFIX}/eating/days", params={"from": "2026-09-22", "to": "2026-09-22"}, headers=me
    )
    assert [d["day"] for d in single.json()] == ["2026-09-22"]


async def test_a_range_must_run_forwards_and_stay_under_400_days(client: AsyncClient, me):
    def ask(start: str, end: str):
        return client.get(f"{PREFIX}/eating/days", params={"from": start, "to": end}, headers=me)

    backwards = await ask("2026-09-22", "2026-09-21")
    assert backwards.status_code == 400 and backwards.json()["detail"] == "from is after to"
    # 2025-08-17 → 2026-09-21 is exactly 400 days apart; one more is refused
    assert (await ask("2025-08-17", DAY)).status_code == 200
    too_long = await ask("2025-08-16", DAY)
    assert too_long.status_code == 400 and too_long.json()["detail"] == "range is too long"
    assert (
        await client.get(f"{PREFIX}/eating/days", params={"from": DAY}, headers=me)
    ).status_code == 422
    assert (await ask("yesterday", DAY)).status_code == 422


# --- Numbers held still -------------------------------------------------------


async def test_a_meal_keeps_the_numbers_it_was_eaten_with(client: AsyncClient, me):
    """Correcting a food, archiving it, or changing an amount later must never
    move what an earlier day came to by itself."""
    bread = await own_food(client, me, name="Domaći hleb", kcal=250, units={"slice": 40})
    eaten = await meal(client, me, DAY, {"food_id": bread["id"], "quantity": 1, "unit": "slice"})
    assert eaten["kcal"] == 100

    await client.patch(
        f"{PREFIX}/eating/foods/{bread['id']}", json={"kcal": 300, "protein": 20}, headers=me
    )
    assert (await _day(client, me))["totals"]["kcal"] == 100

    # A second slice is the same bread as it was that day: 2 × 40 g at 250 kcal
    item = eaten["items"][0]["id"]
    two = await client.patch(
        f"{PREFIX}/eating/meals/{eaten['id']}/items/{item}", json={"quantity": 2}, headers=me
    )
    assert two.json()["kcal"] == 200

    await client.patch(f"{PREFIX}/eating/foods/{bread['id']}", json={"archived": True}, headers=me)
    day = await _day(client, me)
    assert day["totals"]["kcal"] == 200
    assert day["meals"][0]["items"][0]["label"] == "Domaći hleb"

    # A new meal from today on uses today's numbers
    today = await meal(client, me, "2026-09-22", {"food_id": bread["id"], "quantity": 100})
    assert today["kcal"] == 300


async def test_reseeding_the_shared_foods_leaves_past_meals_alone(
    client: AsyncClient, me, seeds, session: AsyncSession
):
    banana = await find_food(client, me, "banana", "Banana")
    eaten = await meal(client, me, DAY, {"food_id": banana["id"], "quantity": 100})
    assert eaten["kcal"] == 89

    corrected = [{**food, "kcal": 95} if food["key"] == "banana" else food for food in SMALL_PANTRY]
    seeds.write_text(json.dumps(corrected), encoding="utf-8")
    await eating_seed.seed_foods(session)
    await session.commit()

    assert (await find_food(client, me, "banana", "Banana"))["kcal"] == 95
    assert (await _day(client, me))["totals"]["kcal"] == 89
    later = await meal(client, me, "2026-09-22", {"food_id": banana["id"], "quantity": 100})
    assert later["kcal"] == 95


# --- Targets ------------------------------------------------------------------


async def test_targets_are_bounded(client: AsyncClient, me):
    for patch in (
        {"target_kcal": -1},
        {"target_kcal": 20001},
        {"target_protein": 1001},
        {"target_carbs": 2001},
        {"target_fat": 1001},
    ):
        response = await client.patch(f"{PREFIX}/eating/settings", json=patch, headers=me)
        assert response.status_code == 422, patch

    set_all = await client.patch(
        f"{PREFIX}/eating/settings",
        json={"target_kcal": 2300, "target_protein": 180, "target_carbs": 230, "target_fat": 75},
        headers=me,
    )
    assert set_all.status_code == 200
    target = (await _day(client, me))["target"]
    assert target == {"kcal": 2300, "protein": 180, "carbs": 230, "fat": 75}
