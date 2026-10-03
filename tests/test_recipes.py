"""Recipes: a batch written once and eaten a share at a time — by the
serving or by the piece, from real ingredients or from numbers alone."""

import pytest
from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers, find_food

DAY = "2026-09-21"


@pytest.fixture
async def me(client: AsyncClient, seeds) -> dict:
    return await auth_headers(client, email="cook@example.com")


@pytest.fixture
async def foods(client: AsyncClient, me) -> dict:
    return {
        "egg": await find_food(client, me, "jaje", "Jaje"),
        "oats": await find_food(client, me, "ovsene", "Ovsene pahuljice"),
        "banana": await find_food(client, me, "banana", "Banana"),
    }


async def _recipe(client: AsyncClient, headers: dict, **body) -> dict:
    response = await client.post(f"{PREFIX}/eating/recipes", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _cook(client: AsyncClient, headers: dict, recipe_id: str, servings: float, **body):
    return await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe_id, "day": DAY, "servings": servings, **body},
        headers=headers,
    )


async def test_a_recipe_adds_up_the_whole_batch(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Ovsena kaša",
        servings=4,
        items=[
            {"food_id": foods["oats"]["id"], "quantity": 200, "unit": "g"},
            {"food_id": foods["banana"]["id"], "quantity": 2, "unit": "piece"},
            {"food_id": foods["egg"]["id"], "quantity": 2, "unit": "piece", "optional": True},
        ],
    )

    assert [(i["label"], i["grams"], i["optional"]) for i in recipe["items"]] == [
        ("Ovsene pahuljice", 200, False),
        ("Banana", 240, False),
        ("Jaje", 110, True),
    ]
    # 758 + 213.6 + 170.5 for all four servings
    assert recipe["kcal"] == 1142.1
    assert (recipe["servings"], recipe["serving_unit"]) == (4, "serving")


async def test_eating_part_of_a_batch_scales_every_ingredient(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Ovsena kaša",
        servings=4,
        items=[
            {"food_id": foods["oats"]["id"], "quantity": 200, "unit": "g"},
            {"food_id": foods["banana"]["id"], "quantity": 2, "unit": "piece"},
        ],
    )

    eaten = await _cook(client, me, recipe["id"], 1.5, at="07:45:00")

    assert eaten.status_code == 201, eaten.text
    body = eaten.json()
    assert (body["title"], body["recipe_id"], body["recipe_title"], body["servings"]) == (
        "Ovsena kaša",
        recipe["id"],
        "Ovsena kaša",
        1.5,
    )
    assert body["at"] == "07:45:00"
    # 1.5 of 4 servings: 75 g oats and three quarters of a banana
    assert [(i["quantity"], i["unit"], i["grams"]) for i in body["items"]] == [
        (75, "g", 75),
        (0.75, "piece", 90),
    ]
    # Each line is rounded to a tenth, and the meal is the sum of its lines
    assert [i["kcal"] for i in body["items"]] == [284.2, 80.1]
    assert body["kcal"] == 364.3


async def test_awkward_fractions_round_sensibly(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Omlet",
        servings=3,
        items=[{"food_id": foods["egg"]["id"], "quantity": 5, "unit": "piece"}],
    )

    third = (await _cook(client, me, recipe["id"], 1)).json()

    assert third["items"][0]["quantity"] == 1.67
    assert third["items"][0]["grams"] == 91.7
    assert third["kcal"] == round(155 * 0.917, 1)


async def test_how_much_of_a_recipe_is_eaten_is_bounded(client: AsyncClient, me, foods):
    recipe = await _recipe(client, me, title="Omlet")
    for servings in (0, -1, 50.5):
        assert (await _cook(client, me, recipe["id"], servings)).status_code == 422, servings
    assert (await _cook(client, me, recipe["id"], 50)).status_code == 201


async def test_a_batch_in_pieces_is_eaten_by_the_piece(client: AsyncClient, me, foods):
    muffins = await _recipe(
        client,
        me,
        title="Mafini",
        servings=12,
        serving_unit="piece",
        items=[
            {"food_id": foods["oats"]["id"], "quantity": 240, "unit": "g"},
            {"food_id": foods["egg"]["id"], "quantity": 6, "unit": "piece"},
        ],
    )

    one = (await _cook(client, me, muffins["id"], 1)).json()

    assert [(i["quantity"], i["grams"]) for i in one["items"]] == [(20, 20), (0.5, 27.5)]
    assert one["servings"] == 1
    assert one["kcal"] == round(muffins["kcal"] / 12, 1)


async def test_a_dish_known_by_its_numbers_is_split_by_its_servings(client: AsyncClient, me):
    """The stated numbers are the whole dish, like the ingredients' total
    is, so a dish written for two and eaten by one person counts half."""
    pie = await _recipe(
        client,
        me,
        title="Pita sa sirom",
        servings=2,
        stated={"kcal": 742, "protein": 24, "carbs": 98, "fat": 25},
    )
    assert pie["items"] == [] and pie["kcal"] == 0
    assert pie["stated"] == {"kcal": 742, "protein": 24, "carbs": 98, "fat": 25}

    half = (await _cook(client, me, pie["id"], 1)).json()
    whole = (await _cook(client, me, pie["id"], 2)).json()

    [item] = half["items"]
    assert (item["label"], item["unit"], item["quantity"], item["food_id"]) == (
        "Pita sa sirom",
        "serving",
        1,
        None,
    )
    assert (half["kcal"], half["protein"], half["carbs"], half["fat"]) == (371, 12, 49, 12.5)
    assert whole["kcal"] == 742


async def test_changing_how_many_servings_of_a_stated_dish_were_eaten(client: AsyncClient, me):
    pie = await _recipe(
        client, me, title="Pita sa sirom", servings=2, stated={"kcal": 742, "protein": 24}
    )
    eaten = (await _cook(client, me, pie["id"], 1)).json()
    assert eaten["kcal"] == 371

    item = eaten["items"][0]["id"]
    second = await client.patch(
        f"{PREFIX}/eating/meals/{eaten['id']}/items/{item}", json={"quantity": 2}, headers=me
    )

    assert second.json()["kcal"] == 742
    half = await client.patch(
        f"{PREFIX}/eating/meals/{eaten['id']}/items/{item}", json={"quantity": 0.5}, headers=me
    )
    assert half.json()["kcal"] == 185.5


async def test_a_stated_dish_for_three_is_a_third_without_rounding_it_away(client: AsyncClient, me):
    stew = await _recipe(client, me, title="Gulaš", servings=3, stated={"kcal": 1000})

    eaten = (await _cook(client, me, stew["id"], 1)).json()

    assert eaten["kcal"] == 333.3


async def test_real_ingredients_win_over_stated_numbers(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Omlet",
        stated={"kcal": 1000},
        items=[{"food_id": foods["egg"]["id"], "quantity": 2, "unit": "piece"}],
    )

    eaten = (await _cook(client, me, recipe["id"], 1)).json()

    assert eaten["kcal"] == 170.5
    assert [i["label"] for i in eaten["items"]] == ["Jaje"]


async def test_a_recipe_follows_its_foods_but_a_cooked_meal_does_not(client: AsyncClient, me):
    """The recipe is a live sum of today's foods; the meal is a copy of the
    day it was cooked."""
    flour = (
        await client.post(
            f"{PREFIX}/eating/foods",
            json={"name": "Moje brašno", "kcal": 340, "protein": 10, "carbs": 70, "fat": 1},
            headers=me,
        )
    ).json()
    bread = await _recipe(
        client, me, title="Hleb", items=[{"food_id": flour["id"], "quantity": 500}]
    )
    assert bread["kcal"] == 1700
    cooked = (await _cook(client, me, bread["id"], 1)).json()

    await client.patch(f"{PREFIX}/eating/foods/{flour['id']}", json={"kcal": 360}, headers=me)

    again = (await client.get(f"{PREFIX}/eating/recipes/{bread['id']}", headers=me)).json()
    assert again["kcal"] == 1800
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    assert [m["kcal"] for m in day["meals"]] == [cooked["kcal"]] == [1700]


async def test_ingredients_without_a_food_or_a_known_unit_are_still_kept(client: AsyncClient, me):
    recipe = await _recipe(
        client,
        me,
        title="Salata",
        items=[
            {"label": "Malo soli", "quantity": 1, "unit": "pinch", "optional": True},
            {"label": "", "quantity": 50, "unit": "fistful"},
        ],
    )

    assert [(i["label"], i["unit"], i["grams"], i["food_id"]) for i in recipe["items"]] == [
        ("Malo soli", "pinch", 1, None),
        ("Ingredient", "g", 50, None),
    ]
    assert recipe["kcal"] == 0


async def test_editing_a_recipe_clears_only_what_may_be_empty(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Omlet",
        subtitle="sa paprikom",
        servings=2,
        minutes=10,
        steps=["Umutiti", "Ispržiti"],
        note="Bez soli",
        items=[{"food_id": foods["egg"]["id"], "quantity": 3, "unit": "piece"}],
    )
    path = f"{PREFIX}/eating/recipes/{recipe['id']}"

    cleared = (
        await client.patch(
            path,
            json={
                "subtitle": None,
                "note": None,
                "minutes": None,
                "title": None,
                "servings": None,
                "steps": None,
                "items": None,
            },
            headers=me,
        )
    ).json()
    assert (cleared["subtitle"], cleared["note"], cleared["minutes"]) == (None, None, None)
    # Nulls never blank a title, a serving count, the steps or the ingredients
    assert (cleared["title"], cleared["servings"], cleared["steps"]) == (
        "Omlet",
        2,
        ["Umutiti", "Ispržiti"],
    )
    assert len(cleared["items"]) == 1

    rewritten = (
        await client.patch(
            path,
            json={
                "title": "Kajgana",
                "steps": [],
                "servings": 1,
                "items": [
                    {"food_id": foods["egg"]["id"], "quantity": 2, "unit": "piece"},
                    {"food_id": foods["oats"]["id"], "quantity": 2, "unit": "tbsp"},
                ],
            },
            headers=me,
        )
    ).json()
    assert (rewritten["title"], rewritten["steps"], rewritten["servings"]) == ("Kajgana", [], 1)
    assert [(i["label"], i["grams"]) for i in rewritten["items"]] == [
        ("Jaje", 110),
        ("Ovsene pahuljice", 20),
    ]
    assert (await client.get(path, headers=me)).json()["title"] == "Kajgana"


async def test_what_a_recipe_accepts_is_bounded(client: AsyncClient, me):
    cases = {
        "no title": {},
        "empty title": {"title": ""},
        "long title": {"title": "x" * 161},
        "no servings": {"title": "x", "servings": 0},
        "too many servings": {"title": "x", "servings": 51},
        "a muffin unit": {"title": "x", "serving_unit": "muffin"},
        "ten hours": {"title": "x", "minutes": 601},
        "negative kcal": {"title": "x", "stated": {"kcal": -1}},
        "101 ingredients": {"title": "x", "items": [{"label": "y", "quantity": 1}] * 101},
    }
    for case, body in cases.items():
        response = await client.post(f"{PREFIX}/eating/recipes", json=body, headers=me)
        assert response.status_code == 422, case


async def test_the_recipe_book_is_sorted_and_searchable(client: AsyncClient, me):
    for title, subtitle in (
        ("Palačinke", "sa džemom"),
        ("Burek", None),
        ("Čorba", "pileća"),
        ("Ajvar", None),
    ):
        await _recipe(client, me, title=title, subtitle=subtitle)

    def titles(response) -> list[str]:
        return [recipe["title"] for recipe in response.json()]

    listed = await client.get(f"{PREFIX}/eating/recipes", headers=me)
    assert titles(listed)[:2] == ["Ajvar", "Burek"]

    def search(**params):
        return client.get(f"{PREFIX}/eating/recipes", params=params, headers=me)

    # Written without the diacritics, found with them — subtitles count too
    assert titles(await search(q="palacinke")) == ["Palačinke"]
    assert titles(await search(q="DZEM")) == ["Palačinke"]
    assert titles(await search(q="pilec")) == ["Čorba"]
    assert titles(await search(q="nothing like it")) == []
    assert len((await search(limit=2)).json()) == 2

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["recipes"] == 4


async def test_deleting_a_recipe_keeps_what_was_cooked_from_it(client: AsyncClient, me, foods):
    recipe = await _recipe(
        client,
        me,
        title="Omlet",
        items=[{"food_id": foods["egg"]["id"], "quantity": 2, "unit": "piece"}],
    )
    cooked = (await _cook(client, me, recipe["id"], 1)).json()

    assert (
        await client.delete(f"{PREFIX}/eating/recipes/{recipe['id']}", headers=me)
    ).status_code == 204
    assert (
        await client.get(f"{PREFIX}/eating/recipes/{recipe['id']}", headers=me)
    ).status_code == 404
    assert (await _cook(client, me, recipe["id"], 1)).status_code == 404

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    [kept] = day["meals"]
    assert (kept["id"], kept["recipe_id"], kept["recipe_title"]) == (cooked["id"], None, "Omlet")
    assert kept["kcal"] == 170.5
