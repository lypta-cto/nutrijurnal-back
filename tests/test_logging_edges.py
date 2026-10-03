"""The edges of logging fast: stars on foods that come and go, what counts
as recent, copies that stand on their own, and Undo after the world moved."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food
from app.services import nutrition, reminders
from tests.helpers import PREFIX, auth_headers, own_food

DAY = "2026-09-21"
NEXT = "2026-09-22"
NOWHERE = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def egg(session: AsyncSession) -> str:
    food = Food(
        name="Jaje",
        search_key=nutrition.key_of("Jaje"),
        source="seed",
        kcal=155,
        protein=13,
        carbs=1.1,
        fat=11,
        units={"piece": 55},
    )
    session.add(food)
    await session.commit()
    return str(food.id)


@pytest.fixture
async def me(client: AsyncClient) -> dict:
    return await auth_headers(client, "fast@example.com")


async def meal(client: AsyncClient, headers: dict, day: str = DAY, **body) -> dict:
    response = await client.post(
        f"{PREFIX}/eating/meals", json={"day": day, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def quick(client: AsyncClient, headers: dict) -> dict:
    return (await client.get(f"{PREFIX}/eating/foods/quick", headers=headers)).json()


# --- Stars ------------------------------------------------------------------------


async def test_a_food_that_does_not_exist_can_not_be_starred(client: AsyncClient, me):
    path = f"{PREFIX}/eating/foods/{NOWHERE}/favourite"

    assert (await client.put(path, headers=me)).status_code == 404
    assert (await client.delete(path, headers=me)).status_code == 404


async def test_taking_away_a_star_that_was_never_there_is_harmless(client: AsyncClient, me, egg):
    response = await client.delete(f"{PREFIX}/eating/foods/{egg}/favourite", headers=me)

    assert response.status_code == 200
    assert response.json()["favourite"] is False


async def test_an_archived_food_leaves_the_quick_lists_but_not_the_diary(client: AsyncClient, me):
    bread = await own_food(client, me, name="Domaći hleb")
    await client.put(f"{PREFIX}/eating/foods/{bread['id']}/favourite", headers=me)
    eaten = await meal(client, me, items=[{"food_id": bread["id"], "quantity": 80, "unit": "g"}])

    await client.patch(f"{PREFIX}/eating/foods/{bread['id']}", json={"archived": True}, headers=me)

    lists = await quick(client, me)
    assert lists["favourites"] == [] and lists["recent"] == []
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    assert day["meals"][0]["kcal"] == eaten["kcal"] == 200

    # Brought back, it is still starred
    await client.patch(f"{PREFIX}/eating/foods/{bread['id']}", json={"archived": False}, headers=me)
    assert [food["name"] for food in (await quick(client, me))["favourites"]] == ["Domaći hleb"]


# --- Recent -----------------------------------------------------------------------------


async def test_a_plate_known_only_by_its_numbers_is_not_a_recent_food(client: AsyncClient, me, egg):
    await meal(
        client,
        me,
        items=[
            {"label": "Kolač u kancelariji", "macros": {"kcal": 350}},
            {"food_id": egg, "quantity": 2, "unit": "piece"},
        ],
    )

    assert [food["name"] for food in (await quick(client, me))["recent"]] == ["Jaje"]


async def test_recent_foods_are_capped_at_the_limit_asked_for(client: AsyncClient, me):
    for index in range(5):
        food = await own_food(client, me, name=f"Hrana {index}")
        await meal(
            client,
            me,
            day=f"2026-09-{10 + index:02d}",
            items=[{"food_id": food["id"], "quantity": 100, "unit": "g"}],
        )

    capped = await client.get(f"{PREFIX}/eating/foods/quick", params={"limit": 3}, headers=me)

    # The latest days first
    assert [food["name"] for food in capped.json()["recent"]] == ["Hrana 4", "Hrana 3", "Hrana 2"]
    for limit in (0, 51):
        response = await client.get(
            f"{PREFIX}/eating/foods/quick", params={"limit": limit}, headers=me
        )
        assert response.status_code == 422


# --- Copies -------------------------------------------------------------------------------


async def test_a_copy_stands_on_its_own(client: AsyncClient, me, egg):
    original = await meal(client, me, items=[{"food_id": egg, "quantity": 2, "unit": "piece"}])
    copy = (
        await client.post(
            f"{PREFIX}/eating/meals/{original['id']}/copy", json={"day": DAY}, headers=me
        )
    ).json()

    line = copy["items"][0]["id"]
    await client.patch(
        f"{PREFIX}/eating/meals/{copy['id']}/items/{line}", json={"quantity": 3}, headers=me
    )
    await client.delete(f"{PREFIX}/eating/meals/{original['id']}", headers=me)

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    assert [entry["id"] for entry in day["meals"]] == [copy["id"]]
    assert day["meals"][0]["items"][0]["quantity"] == 3
    assert copy["items"][0]["id"] != original["items"][0]["id"]


async def test_a_copy_leaves_the_recording_with_the_original(client: AsyncClient, me):
    said = await meal(client, me, title="Rekao sam", items=[])
    await client.post(
        f"{PREFIX}/eating/meals/{said['id']}/voice",
        files={"file": ("voice.webm", b"words", "audio/webm")},
        headers=me,
    )

    copy = await client.post(
        f"{PREFIX}/eating/meals/{said['id']}/copy", json={"day": NEXT}, headers=me
    )

    assert copy.json()["has_voice"] is False
    assert (
        await client.get(f"{PREFIX}/eating/meals/{copy.json()['id']}/voice", headers=me)
    ).status_code == 404


async def test_a_meal_cooked_from_a_recipe_is_copied_with_its_recipe(client: AsyncClient, me):
    recipe = await client.post(
        f"{PREFIX}/eating/recipes",
        json={"title": "Sarma", "servings": 4, "stated": {"kcal": 2000}},
        headers=me,
    )
    cooked = await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe.json()["id"], "day": DAY, "servings": 1},
        headers=me,
    )

    copy = await client.post(
        f"{PREFIX}/eating/meals/{cooked.json()['id']}/copy", json={"day": NEXT}, headers=me
    )

    body = copy.json()
    assert (body["recipe_id"], body["recipe_title"]) == (recipe.json()["id"], "Sarma")
    assert body["kcal"] == cooked.json()["kcal"] == 500


async def test_copying_a_day_where_only_someone_else_ate_finds_nothing(
    client: AsyncClient, me, egg
):
    other = await auth_headers(client, "other-eater@example.com")
    await meal(client, other, items=[{"food_id": egg, "quantity": 2, "unit": "piece"}])

    response = await client.post(
        f"{PREFIX}/eating/days/{NEXT}/copy", json={"from_day": DAY}, headers=me
    )

    assert response.status_code == 404
    assert (await client.get(f"{PREFIX}/eating/days/{NEXT}", headers=me)).json()["meals"] == []


@pytest.mark.parametrize(
    "body",
    [{}, {"day": "yesterday"}, {"day": NEXT, "slot": "brunch"}, {"day": NEXT, "at": "25:00"}],
)
async def test_a_copy_needs_a_real_day_slot_and_time(client: AsyncClient, me, egg, body):
    original = await meal(client, me, items=[{"food_id": egg, "quantity": 1, "unit": "piece"}])

    response = await client.post(
        f"{PREFIX}/eating/meals/{original['id']}/copy", json=body, headers=me
    )

    assert response.status_code == 422


async def test_a_meal_that_does_not_exist_can_not_be_copied(client: AsyncClient, me):
    response = await client.post(
        f"{PREFIX}/eating/meals/{NOWHERE}/copy", json={"day": DAY}, headers=me
    )

    assert response.status_code == 404


# --- Undo after the world moved -------------------------------------------------------------


async def test_a_meal_comes_back_without_a_recipe_deleted_since(client: AsyncClient, me):
    recipe = await client.post(
        f"{PREFIX}/eating/recipes",
        json={"title": "Gulaš", "servings": 2, "stated": {"kcal": 1200}},
        headers=me,
    )
    cooked = (
        await client.post(
            f"{PREFIX}/eating/meals/from-recipe",
            json={"recipe_id": recipe.json()["id"], "day": DAY, "servings": 1},
            headers=me,
        )
    ).json()
    await client.delete(f"{PREFIX}/eating/meals/{cooked['id']}", headers=me)
    await client.delete(f"{PREFIX}/eating/recipes/{recipe.json()['id']}", headers=me)

    restored = await client.post(f"{PREFIX}/eating/meals/{cooked['id']}/restore", headers=me)

    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["recipe_id"] is None
    assert (body["title"], body["recipe_title"], body["kcal"]) == (
        cooked["title"],
        "Gulaš",
        cooked["kcal"],
    )


async def test_a_meal_purged_from_the_undo_shelf_is_gone_for_good(client: AsyncClient, me):
    gone = await meal(client, me, items=[{"label": "Burek", "macros": {"kcal": 700}}])
    await client.delete(f"{PREFIX}/eating/meals/{gone['id']}", headers=me)

    await reminders.tidy(datetime(2099, 1, 1, tzinfo=UTC))

    response = await client.post(f"{PREFIX}/eating/meals/{gone['id']}/restore", headers=me)
    assert response.status_code == 404
    assert response.json()["detail"] == "Nothing to restore"


async def test_a_meal_deleted_restored_and_deleted_again_can_be_undone_again(
    client: AsyncClient, me
):
    plate = await meal(client, me, items=[{"label": "Pita", "macros": {"kcal": 400}}])
    path = f"{PREFIX}/eating/meals/{plate['id']}"

    await client.delete(path, headers=me)
    await client.post(f"{path}/restore", headers=me)
    await client.delete(path, headers=me)
    again = await client.post(f"{path}/restore", headers=me)

    assert again.status_code == 200
    assert again.json()["kcal"] == 400
