"""The app is public: a stranger who signs up must never see, change or even
confirm the existence of anyone else's diary. Every attempt below is made by
B against rows that belong to A, and each one must look exactly like asking
for something that does not exist."""

import pytest
from httpx import AsyncClient

from app.services import eating_report
from tests.helpers import PREFIX, auth_headers, find_food, meal, own_food

DAY = "2026-09-21"
AUDIO = b"OggS-fake-voice"


@pytest.fixture
async def alice(client: AsyncClient, seeds) -> dict:
    """A's whole diary: a private food with a barcode, a meal eaten from it
    with a voice note on it, and a recipe built from it."""
    headers = await auth_headers(client, email="alice@example.com", full_name="Alice")
    ajvar = await own_food(
        client,
        headers,
        name="Bakin ajvar",
        kcal=120,
        protein=2,
        carbs=10,
        fat=8,
        barcode="5901234123457",
    )
    lunch = await meal(
        client,
        headers,
        DAY,
        {"food_id": ajvar["id"], "quantity": 100, "unit": "g"},
        title="Alice's lunch",
        note="private words",
    )
    voiced = await client.post(
        f"{PREFIX}/eating/meals/{lunch['id']}/voice",
        files={"file": ("note.ogg", AUDIO, "audio/ogg")},
        headers=headers,
    )
    assert voiced.status_code == 200, voiced.text
    recipe = (
        await client.post(
            f"{PREFIX}/eating/recipes",
            json={
                "title": "Alice's pinđur",
                "items": [{"food_id": ajvar["id"], "quantity": 200, "unit": "g"}],
            },
            headers=headers,
        )
    ).json()
    return {"headers": headers, "food": ajvar, "meal": lunch, "recipe": recipe}


@pytest.fixture
async def bob(client: AsyncClient) -> dict:
    return await auth_headers(client, email="bob@example.com", full_name="Bob")


async def _alice_is_untouched(client: AsyncClient, alice: dict) -> None:
    headers = alice["headers"]
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    [lunch] = day["meals"]
    assert lunch["title"] == "Alice's lunch" and lunch["note"] == "private words"
    assert [(item["quantity"], item["kcal"]) for item in lunch["items"]] == [(100, 120)]
    assert lunch["has_voice"] is True
    played = await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=headers)
    assert played.content == AUDIO
    recipe = await client.get(f"{PREFIX}/eating/recipes/{alice['recipe']['id']}", headers=headers)
    assert recipe.json()["title"] == "Alice's pinđur" and recipe.json()["kcal"] == 240
    mine = await client.get(f"{PREFIX}/eating/foods", params={"mine": True}, headers=headers)
    assert [(food["name"], food["kcal"], food["archived"]) for food in mine.json()] == [
        ("Bakin ajvar", 120, False)
    ]


async def test_reading_someone_elses_diary_finds_nothing(client: AsyncClient, alice, bob):
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=bob)).json()
    assert day["meals"] == [] and day["totals"]["kcal"] == 0
    days = await client.get(
        f"{PREFIX}/eating/days", params={"from": "2026-09-01", "to": "2026-09-30"}, headers=bob
    )
    assert days.json() == []
    assert (await client.get(f"{PREFIX}/eating/recipes", headers=bob)).json() == []
    assert (
        await client.get(f"{PREFIX}/eating/recipes", params={"q": "pinđur"}, headers=bob)
    ).json() == []

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=bob)).json()
    assert settings["recipes"] == 0


async def test_a_private_food_is_invisible_to_everyone_else(client: AsyncClient, alice, bob):
    for params in ({"q": "ajvar"}, {"q": "bakin"}, {"mine": True}, {}, {"limit": 200}):
        names = [
            food["name"]
            for food in (
                await client.get(f"{PREFIX}/eating/foods", params=params, headers=bob)
            ).json()
        ]
        assert "Bakin ajvar" not in names, params

    parsed = (
        await client.post(f"{PREFIX}/eating/parse", json={"text": "100g ajvara"}, headers=bob)
    ).json()
    assert parsed["items"] == [] and parsed["unknown"] == ["100g ajvara"]

    # Editing or archiving it by its id answers as if there were no such food
    for patch in ({"kcal": 1}, {"archived": True}, {"name": "Mine now"}):
        response = await client.patch(
            f"{PREFIX}/eating/foods/{alice['food']['id']}", json=patch, headers=bob
        )
        assert response.status_code == 404, patch
        assert response.json()["detail"] == "No such food"

    await _alice_is_untouched(client, alice)


async def test_a_private_food_lends_nothing_to_someone_who_guesses_its_id(
    client: AsyncClient, alice, bob
):
    """Its id is not a key to its numbers or its name: a meal, an item or a
    recipe built on it by someone else comes out empty."""
    stolen = alice["food"]["id"]

    built = await meal(client, bob, DAY, {"food_id": stolen, "quantity": 100, "unit": "g"})
    [item] = built["items"]
    assert item["food_id"] is None and item["kcal"] == 0
    assert "ajvar" not in item["label"].lower()

    added = await client.post(
        f"{PREFIX}/eating/meals/{built['id']}/items",
        json={"food_id": stolen, "quantity": 50},
        headers=bob,
    )
    assert added.status_code == 200
    assert added.json()["items"][-1]["food_id"] is None and added.json()["kcal"] == 0

    swapped = await client.patch(
        f"{PREFIX}/eating/meals/{built['id']}/items/{item['id']}",
        json={"food_id": stolen},
        headers=bob,
    )
    assert swapped.status_code == 404

    recipe = (
        await client.post(
            f"{PREFIX}/eating/recipes",
            json={"title": "Copy", "items": [{"food_id": stolen, "quantity": 100}]},
            headers=bob,
        )
    ).json()
    assert recipe["items"][0]["food_id"] is None and recipe["kcal"] == 0
    assert "ajvar" not in recipe["items"][0]["label"].lower()


async def test_someone_elses_meal_can_not_be_touched_in_any_way(client: AsyncClient, alice, bob):
    path = f"{PREFIX}/eating/meals/{alice['meal']['id']}"
    item = alice["meal"]["items"][0]["id"]
    attempts = {
        "rename": client.patch(path, json={"title": "Bob's now"}, headers=bob),
        "move": client.patch(path, json={"day": "2026-01-01"}, headers=bob),
        "add item": client.post(f"{path}/items", json={"label": "x"}, headers=bob),
        "edit item": client.patch(f"{path}/items/{item}", json={"quantity": 9}, headers=bob),
        "remove item": client.delete(f"{path}/items/{item}", headers=bob),
        "play voice": client.get(f"{path}/voice", headers=bob),
        "replace voice": client.post(
            f"{path}/voice", files={"file": ("x.ogg", b"bob", "audio/ogg")}, headers=bob
        ),
        "drop voice": client.delete(f"{path}/voice", headers=bob),
        "delete": client.delete(path, headers=bob),
    }
    for name, attempt in attempts.items():
        response = await attempt
        assert response.status_code == 404, name
        assert response.json()["detail"] == "No such meal", name

    await _alice_is_untouched(client, alice)


async def test_an_item_only_answers_under_its_own_meal(client: AsyncClient, alice, bob):
    """Even its owner can not reach an item through a different meal — and
    a stranger can not reach it through a meal of their own."""
    item = alice["meal"]["items"][0]["id"]
    other = await meal(client, alice["headers"], "2026-09-22", title="Dinner")
    bobs = await meal(client, bob, DAY, title="Bob's")

    for owner, meal_id in ((alice["headers"], other["id"]), (bob, bobs["id"])):
        path = f"{PREFIX}/eating/meals/{meal_id}/items/{item}"
        edited = await client.patch(path, json={"quantity": 1}, headers=owner)
        removed = await client.delete(path, headers=owner)
        assert edited.status_code == removed.status_code == 404
        assert edited.json()["detail"] == "No such item"

    await _alice_is_untouched(client, alice)


async def test_someone_elses_recipe_can_not_be_read_cooked_or_changed(
    client: AsyncClient, alice, bob
):
    path = f"{PREFIX}/eating/recipes/{alice['recipe']['id']}"
    attempts = {
        "read": client.get(path, headers=bob),
        "edit": client.patch(path, json={"title": "Bob's"}, headers=bob),
        "replace items": client.patch(path, json={"items": []}, headers=bob),
        "delete": client.delete(path, headers=bob),
        "cook": client.post(
            f"{PREFIX}/eating/meals/from-recipe",
            json={"recipe_id": alice["recipe"]["id"], "day": DAY},
            headers=bob,
        ),
    }
    for name, attempt in attempts.items():
        response = await attempt
        assert response.status_code == 404, name
        assert response.json()["detail"] == "No such recipe", name

    assert (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=bob)).json()["meals"] == []
    await _alice_is_untouched(client, alice)


async def test_an_export_holds_only_the_exporters_own_days(
    client: AsyncClient, alice, bob, monkeypatch
):
    printed: list[dict] = []
    real_build_pdf = eating_report.build_pdf

    def spy(days, **options):
        printed.append({"days": days, **options})
        return real_build_pdf(days, **options)

    monkeypatch.setattr(eating_report, "build_pdf", spy)
    span = {"from": "2026-09-01", "to": "2026-09-30"}

    for headers in (alice["headers"], bob):
        pdf = await client.get(
            f"{PREFIX}/eating/export", params={**span, "format": "pdf"}, headers=headers
        )
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    alices, bobs = printed
    assert alices["who"] == "Alice"
    [(day, meals, _)] = alices["days"]
    assert str(day) == DAY and [meal.title for meal in meals] == ["Alice's lunch"]
    assert bobs["who"] == "Bob" and bobs["days"] == []

    csv_text = (
        await client.get(f"{PREFIX}/eating/export", params={**span, "format": "csv"}, headers=bob)
    ).content.decode("utf-8-sig")
    assert csv_text.splitlines()[1:] == []
    assert "Alice" not in csv_text and "ajvar" not in csv_text


async def test_scanning_a_barcode_someone_else_saved_never_returns_their_food(
    client: AsyncClient, alice, bob, monkeypatch, open_food_facts
):
    from app.services import food_lookup

    monkeypatch.setattr(food_lookup, "read_barcode", lambda content: "5901234123457")
    photo = {"photo": ("jar.jpg", b"jpeg", "image/jpeg")}

    # Not in Open Food Facts either: a miss, never Alice's row
    missed = (await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=bob)).json()
    assert missed["found"] is False and missed["food"] is None

    open_food_facts.products["5901234123457"] = {
        "product_name": "Ajvar ljuti",
        "nutriments": {"energy-kcal_100g": 95, "proteins_100g": 1.5},
    }
    found = (await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=bob)).json()
    assert found["found"] is True and found["food"]["mine"] is True
    assert found["food"]["id"] != alice["food"]["id"] and found["food"]["kcal"] == 95

    # Alice scanning it still gets her own, without asking the internet
    asked = len(open_food_facts.requests)
    hers = (
        await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=alice["headers"])
    ).json()
    assert hers["food"]["id"] == alice["food"]["id"]
    assert len(open_food_facts.requests) == asked

    await _alice_is_untouched(client, alice)


async def test_shared_foods_are_everyones_and_nobodys(client: AsyncClient, alice, bob):
    for headers in (alice["headers"], bob):
        banana = await find_food(client, headers, "banana", "Banana")
        assert banana["mine"] is False and banana["source"] == "seed"
        for patch in ({"kcal": 1}, {"archived": True}, {"units": {"piece": 1}}):
            refused = await client.patch(
                f"{PREFIX}/eating/foods/{banana['id']}", json=patch, headers=headers
            )
            assert refused.status_code == 403, patch
            assert "Shared foods can't be edited" in refused.json()["detail"]

        # Both can eat it, at the same numbers
        eaten = await meal(
            client, headers, "2026-09-22", {"food_id": banana["id"], "quantity": 100}
        )
        assert eaten["kcal"] == 89

    # Neither change touched the shared row
    assert (await find_food(client, bob, "banana", "Banana"))["kcal"] == 89
