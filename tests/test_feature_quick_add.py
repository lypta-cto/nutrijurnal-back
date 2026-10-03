"""Logging fast: meals land in breakfast, lunch, dinner or a snack; starred and
recent foods come first; a meal or a whole day can be written down again."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food
from app.services import nutrition
from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"
NEXT = "2026-09-22"


@pytest.fixture
async def pantry(session: AsyncSession) -> dict[str, str]:
    """Three shared staples, the way the seed leaves them."""
    rows = {
        "Jaje": dict(kcal=155, protein=13, carbs=1.1, fat=11, units={"piece": 55}),
        "Jogurt": dict(kcal=61, protein=3.5, carbs=4.7, fat=3.2, units=None),
        "Jabuka": dict(kcal=52, protein=0.3, carbs=14, fat=0.2, units={"piece": 180}),
    }
    ids = {}
    for name, numbers in rows.items():
        food = Food(name=name, search_key=nutrition.key_of(name), source="seed", **numbers)
        session.add(food)
        await session.flush()
        ids[name] = str(food.id)
    await session.commit()
    return ids


async def _meal(client: AsyncClient, headers: dict, **body) -> dict:
    response = await client.post(
        f"{PREFIX}/eating/meals", json={"day": DAY, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_meal_takes_its_slot_from_the_page_the_clock_or_its_name(
    client: AsyncClient, pantry: dict
):
    headers = await auth_headers(client, "slots@example.com")
    egg = {"food_id": pantry["Jaje"], "quantity": 2, "unit": "piece"}

    said = await _meal(client, headers, slot="dinner", at="08:00", items=[egg])
    timed = await _meal(client, headers, at="13:15", items=[egg])
    named = await _meal(client, headers, title="Večera kod mame", items=[egg])
    nothing = await _meal(client, headers, items=[egg])

    assert [said["slot"], timed["slot"], named["slot"], nothing["slot"]] == [
        "dinner",
        "lunch",
        "dinner",
        "snack",
    ]


async def test_an_untitled_meal_is_named_after_its_one_food_or_its_slot(
    client: AsyncClient, pantry: dict
):
    headers = await auth_headers(client, "titles@example.com")
    egg = {"food_id": pantry["Jaje"], "quantity": 2, "unit": "piece"}
    apple = {"food_id": pantry["Jabuka"], "quantity": 1, "unit": "piece"}

    single = await _meal(client, headers, slot="breakfast", items=[egg])
    plate = await _meal(client, headers, slot="breakfast", items=[egg, apple])
    renamed = await client.patch(
        f"{PREFIX}/eating/meals/{plate['id']}",
        json={"title": "Sunday plate", "slot": "lunch"},
        headers=headers,
    )

    assert single["title"] == "Jaje"
    assert plate["title"] == "Breakfast"
    # Renamed and moved, the meal keeps the name it was given
    assert renamed.json()["title"] == "Sunday plate"
    assert renamed.json()["slot"] == "lunch"


async def test_starring_a_food_puts_it_first_and_only_for_that_person(
    client: AsyncClient, pantry: dict
):
    mine = await auth_headers(client, "stars@example.com")
    theirs = await auth_headers(client, "nostars@example.com")

    starred = await client.put(f"{PREFIX}/eating/foods/{pantry['Jogurt']}/favourite", headers=mine)
    assert starred.status_code == 200 and starred.json()["favourite"] is True
    # Twice is the same as once
    again = await client.put(f"{PREFIX}/eating/foods/{pantry['Jogurt']}/favourite", headers=mine)
    assert again.status_code == 200

    # A bare "j" matches all three equally — the star breaks the tie
    found = await client.get(f"{PREFIX}/eating/foods", params={"q": "j"}, headers=mine)
    assert found.json()[0]["name"] == "Jogurt"

    quick = (await client.get(f"{PREFIX}/eating/foods/quick", headers=mine)).json()
    assert [food["name"] for food in quick["favourites"]] == ["Jogurt"]

    other = (await client.get(f"{PREFIX}/eating/foods/quick", headers=theirs)).json()
    assert other["favourites"] == []
    listed = await client.get(f"{PREFIX}/eating/foods", params={"q": "jogurt"}, headers=theirs)
    assert listed.json()[0]["favourite"] is False

    unstarred = await client.delete(
        f"{PREFIX}/eating/foods/{pantry['Jogurt']}/favourite", headers=mine
    )
    assert unstarred.json()["favourite"] is False
    quick = (await client.get(f"{PREFIX}/eating/foods/quick", headers=mine)).json()
    assert quick["favourites"] == []


async def test_nobody_can_star_someone_elses_private_food(client: AsyncClient):
    owner = await auth_headers(client, "owner@example.com")
    stranger = await auth_headers(client, "stranger@example.com")
    food = await client.post(
        f"{PREFIX}/eating/foods", json={"name": "Moj hleb", "kcal": 250}, headers=owner
    )

    response = await client.put(
        f"{PREFIX}/eating/foods/{food.json()['id']}/favourite", headers=stranger
    )

    assert response.status_code == 404


async def test_recent_foods_remember_the_amount_and_stay_private(client: AsyncClient, pantry: dict):
    headers = await auth_headers(client, "recent@example.com")
    other = await auth_headers(client, "other-recent@example.com")
    await _meal(
        client, headers, items=[{"food_id": pantry["Jaje"], "quantity": 2, "unit": "piece"}]
    )
    await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": NEXT,
            "items": [
                {"food_id": pantry["Jogurt"], "quantity": 200, "unit": "g"},
                {"food_id": pantry["Jaje"], "quantity": 3, "unit": "piece"},
            ],
        },
        headers=headers,
    )
    await client.put(f"{PREFIX}/eating/foods/{pantry['Jabuka']}/favourite", headers=headers)

    quick = (await client.get(f"{PREFIX}/eating/foods/quick", headers=headers)).json()

    recent = {food["name"]: food for food in quick["recent"]}
    assert list(recent) == ["Jaje", "Jogurt"]
    # The latest time it was eaten, in the amount it was eaten
    assert (recent["Jaje"]["last_quantity"], recent["Jaje"]["last_unit"]) == (3, "piece")
    assert recent["Jaje"]["last_day"] == NEXT
    assert [food["name"] for food in quick["favourites"]] == ["Jabuka"]

    assert (await client.get(f"{PREFIX}/eating/foods/quick", headers=other)).json()["recent"] == []


async def test_a_copied_meal_adds_up_exactly_as_the_original(client: AsyncClient, pantry: dict):
    headers = await auth_headers(client, "copy@example.com")
    original = await _meal(
        client,
        headers,
        at="08:10",
        title="Doručak",
        note="before the gym",
        items=[
            {"food_id": pantry["Jaje"], "quantity": 2, "unit": "piece"},
            {"label": "Kafa sa mlekom", "macros": {"kcal": 40, "protein": 2, "carbs": 3, "fat": 2}},
        ],
    )

    response = await client.post(
        f"{PREFIX}/eating/meals/{original['id']}/copy", json={"day": NEXT}, headers=headers
    )

    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["id"] != original["id"]
    assert (copy["day"], copy["at"], copy["slot"], copy["title"]) == (
        NEXT,
        "08:10:00",
        "breakfast",
        "Doručak",
    )
    assert copy["kcal"] == original["kcal"] and copy["note"] == "before the gym"
    assert [item["label"] for item in copy["items"]] == ["Jaje", "Kafa sa mlekom"]

    moved = await client.post(
        f"{PREFIX}/eating/meals/{original['id']}/copy",
        json={"day": NEXT, "slot": "snack", "at": "16:00"},
        headers=headers,
    )
    assert (moved.json()["slot"], moved.json()["at"]) == ("snack", "16:00:00")

    stranger = await auth_headers(client, "copycat@example.com")
    stolen = await client.post(
        f"{PREFIX}/eating/meals/{original['id']}/copy", json={"day": NEXT}, headers=stranger
    )
    assert stolen.status_code == 404


async def test_a_whole_day_or_one_slot_of_it_is_written_down_again(
    client: AsyncClient, pantry: dict
):
    headers = await auth_headers(client, "repeat@example.com")
    egg = {"food_id": pantry["Jaje"], "quantity": 2, "unit": "piece"}
    await _meal(client, headers, slot="breakfast", items=[egg])
    await _meal(client, headers, slot="lunch", items=[egg, egg])
    stranger = await auth_headers(client, "repeat-stranger@example.com")
    await _meal(client, stranger, slot="breakfast", items=[egg])

    breakfast = await client.post(
        f"{PREFIX}/eating/days/{NEXT}/copy",
        json={"from_day": DAY, "slot": "breakfast"},
        headers=headers,
    )
    assert breakfast.status_code == 201, breakfast.text
    assert [meal["slot"] for meal in breakfast.json()] == ["breakfast"]

    whole = await client.post(
        f"{PREFIX}/eating/days/2026-09-23/copy", json={"from_day": DAY}, headers=headers
    )
    assert sorted(meal["slot"] for meal in whole.json()) == ["breakfast", "lunch"]

    day = (await client.get(f"{PREFIX}/eating/days/{NEXT}", headers=headers)).json()
    assert len(day["meals"]) == 1

    empty = await client.post(
        f"{PREFIX}/eating/days/{NEXT}/copy",
        json={"from_day": DAY, "slot": "dinner"},
        headers=headers,
    )
    assert empty.status_code == 404
    assert empty.json()["detail"] == "Nothing to copy on that day"

    # The stranger copies their own day, not anyone else's
    theirs = await client.post(
        f"{PREFIX}/eating/days/{NEXT}/copy", json={"from_day": DAY}, headers=stranger
    )
    assert len(theirs.json()) == 1
