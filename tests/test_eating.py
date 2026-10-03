"""The diary: typing a meal in words, cooking a recipe into it, the numbers
holding still afterwards — and every person's diary staying their own."""

import json

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food
from app.services import eating_seed, nutrition
from tests.helpers import PREFIX, SMALL_PANTRY, auth_headers

DAY = "2026-09-21"
FOODS = SMALL_PANTRY


async def _food(client: AsyncClient, headers: dict, q: str, name: str) -> dict:
    found = (await client.get(f"{PREFIX}/eating/foods", params={"q": q}, headers=headers)).json()
    return next(food for food in found if food["name"] == name)


async def test_the_diary_needs_a_signed_in_person(client: AsyncClient):
    for path in ("/eating/settings", f"/eating/days/{DAY}", "/eating/foods", "/eating/recipes"):
        assert (await client.get(f"{PREFIX}{path}")).status_code == 401, path


async def test_targets_and_the_first_run_are_kept_per_person(client: AsyncClient, seeds):
    headers = await auth_headers(client, email="eat1@example.com", full_name="Luka")

    before = (await client.get(f"{PREFIX}/eating/settings", headers=headers)).json()
    # Every diary opens on the shared pantry, with no targets and no recipes
    assert before["target_kcal"] is None and before["onboarded_at"] is None
    assert before["foods"] == len(FOODS) and before["recipes"] == 0

    after = (
        await client.patch(
            f"{PREFIX}/eating/settings",
            json={"target_kcal": 2300, "target_protein": 180, "onboarded": True},
            headers=headers,
        )
    ).json()
    assert after["target_kcal"] == 2300 and after["target_protein"] == 180
    assert after["onboarded_at"] is not None

    # The day reads the target back, and /auth/me says the questions are done
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["target"]["kcal"] == 2300 and day["target"]["fat"] == 0
    me = (await client.get(f"{PREFIX}/auth/me", headers=headers)).json()
    # SQLite hands datetimes back without their zone; the moment is what matters
    assert me["onboarded_at"][:19] == after["onboarded_at"][:19]

    # Answering again never moves when it was first done, and false is ignored
    again = (
        await client.patch(f"{PREFIX}/eating/settings", json={"onboarded": False}, headers=headers)
    ).json()
    assert again["onboarded_at"][:19] == after["onboarded_at"][:19]

    # A target can be cleared again
    cleared = (
        await client.patch(
            f"{PREFIX}/eating/settings",
            json={"target_kcal": None, "target_protein": None},
            headers=headers,
        )
    ).json()
    assert cleared["target_kcal"] is None
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["target"] is None

    # Someone else's settings are untouched by all of that
    other = await auth_headers(client, email="eat1b@example.com")
    theirs = (await client.get(f"{PREFIX}/eating/settings", headers=other)).json()
    assert theirs["target_kcal"] is None and theirs["onboarded_at"] is None


async def test_a_meal_typed_in_plain_words_adds_itself_up(client: AsyncClient, seeds):
    headers = await auth_headers(client, email="eat2@example.com")
    await client.patch(f"{PREFIX}/eating/settings", json={"target_kcal": 2300}, headers=headers)

    parsed = await client.post(
        f"{PREFIX}/eating/parse",
        json={"text": "50g ovsenih, 1 merica whey, 1 banana"},
        headers=headers,
    )
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert body["unknown"] == []
    assert [(item["label"], item["grams"]) for item in body["items"]] == [
        ("Ovsene pahuljice", 50.0),
        ("Whey protein", 30.0),
        ("Banana", 120.0),
    ]
    # 50 g oats + 30 g whey + 120 g banana
    assert round(sum(item["kcal"] for item in body["items"])) == 410

    meal = await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": DAY,
            "at": "08:30:00",
            "title": "Doručak",
            "items": [
                {
                    "food_id": item["food_id"],
                    "label": item["label"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                }
                for item in body["items"]
            ],
        },
        headers=headers,
    )
    assert meal.status_code == 201, meal.text
    saved = meal.json()
    assert round(saved["kcal"]) == 410 and len(saved["items"]) == 3

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert round(day["totals"]["kcal"]) == 410
    assert day["target"]["kcal"] == 2300
    assert [meal["title"] for meal in day["meals"]] == ["Doručak"]

    # A second helping of oats on the same meal moves the day's numbers
    more = await client.post(
        f"{PREFIX}/eating/meals/{saved['id']}/items",
        json={"food_id": body["items"][0]["food_id"], "quantity": 2, "unit": "tbsp"},
        headers=headers,
    )
    assert more.status_code == 200
    assert more.json()["items"][-1]["grams"] == 20.0

    days = (
        await client.get(f"{PREFIX}/eating/days", params={"from": DAY, "to": DAY}, headers=headers)
    ).json()
    assert days[0]["meals"] == 1 and round(days[0]["kcal"]) == 486


async def test_a_plate_known_only_by_its_kcal_counts(client: AsyncClient, seeds):
    """Cake at the office: no food to pick, just a number. It still counts,
    and a second slice later is simply quantity two."""
    headers = await auth_headers(client, email="quick@example.com")

    meal = await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": DAY,
            "title": "Snack",
            "items": [
                {
                    "label": "Cake",
                    "quantity": 1,
                    "macros": {"kcal": 350, "protein": 5, "carbs": 40, "fat": 18},
                }
            ],
        },
        headers=headers,
    )
    assert meal.status_code == 201, meal.text
    [item] = meal.json()["items"]
    assert (item["label"], item["unit"], item["food_id"]) == ("Cake", "serving", None)
    assert item["kcal"] == 350 and item["fat"] == 18

    twice = await client.patch(
        f"{PREFIX}/eating/meals/{meal.json()['id']}/items/{item['id']}",
        json={"quantity": 2},
        headers=headers,
    )
    assert twice.json()["kcal"] == 700

    # Typed numbers are bounded like everything else typed
    wrong = await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": DAY, "items": [{"label": "x", "macros": {"kcal": -5}}]},
        headers=headers,
    )
    assert wrong.status_code == 422


async def test_a_recipe_is_cooked_into_the_diary_and_can_be_rewritten(client: AsyncClient, seeds):
    headers = await auth_headers(client, email="eat3@example.com")
    egg = await _food(client, headers, "jaje", "Jaje")

    made = await client.post(
        f"{PREFIX}/eating/recipes",
        json={
            "title": "Omlet",
            "subtitle": "sa paprikom",
            "minutes": 10,
            "steps": ["Umutiti jaja", "Isprziti"],
            "items": [{"food_id": egg["id"], "quantity": 5, "unit": "piece"}],
        },
        headers=headers,
    )
    assert made.status_code == 201, made.text
    recipe = made.json()
    assert recipe["items"][0]["grams"] == 275 and recipe["items"][0]["label"] == "Jaje"
    # Five eggs, computed from the food table
    assert round(recipe["kcal"]) == 426

    # Half a portion lands in the diary at half the amounts
    half = await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe["id"], "day": DAY, "at": "12:00:00", "servings": 0.5},
        headers=headers,
    )
    assert half.status_code == 201, half.text
    assert half.json()["items"][0]["grams"] == 137.5
    assert half.json()["recipe_title"] == "Omlet"

    # "It calls for five eggs, I use two"
    edited = await client.patch(
        f"{PREFIX}/eating/recipes/{recipe['id']}",
        json={"items": [{"food_id": egg["id"], "quantity": 2, "unit": "piece"}]},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["items"][0]["grams"] == 110.0

    # The meal cooked earlier keeps the amounts it was cooked with
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["meals"][0]["items"][0]["grams"] == 137.5

    # Deleting the recipe leaves the diary as it was, title and all
    gone = await client.delete(f"{PREFIX}/eating/recipes/{recipe['id']}", headers=headers)
    assert gone.status_code == 204
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["meals"][0]["recipe_title"] == "Omlet"
    assert day["meals"][0]["recipe_id"] is None
    assert (await client.get(f"{PREFIX}/eating/recipes", headers=headers)).json() == []


async def test_correcting_a_food_never_moves_what_was_already_eaten(client: AsyncClient, seeds):
    headers = await auth_headers(client, email="eat4@example.com")
    bread = (
        await client.post(
            f"{PREFIX}/eating/foods",
            json={
                "name": "Domaći hleb",
                "kcal": 250,
                "protein": 8,
                "carbs": 50,
                "fat": 2,
                "units": {"slice": 40},
            },
            headers=headers,
        )
    ).json()
    meal = (
        await client.post(
            f"{PREFIX}/eating/meals",
            json={
                "day": DAY,
                "title": "Užina",
                "items": [{"food_id": bread["id"], "quantity": 1, "unit": "slice"}],
            },
            headers=headers,
        )
    ).json()
    assert meal["kcal"] == 100

    fixed = await client.patch(
        f"{PREFIX}/eating/foods/{bread['id']}", json={"kcal": 300}, headers=headers
    )
    assert fixed.status_code == 200 and fixed.json()["kcal"] == 300

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["meals"][0]["kcal"] == 100  # yesterday's sandwich did not change


async def test_shared_foods_are_read_only_and_private_foods_stay_private(
    client: AsyncClient, seeds
):
    mine = await auth_headers(client, email="owner@example.com")
    theirs = await auth_headers(client, email="stranger@example.com")
    banana = await _food(client, mine, "banana", "Banana")
    assert banana["mine"] is False and banana["source"] == "seed"

    # Nobody may change a shared staple — not even by trying to archive it
    for patch in ({"kcal": 1}, {"archived": True}, {"name": "Mine now"}):
        refused = await client.patch(
            f"{PREFIX}/eating/foods/{banana['id']}", json=patch, headers=mine
        )
        assert refused.status_code == 403, patch
    assert (await _food(client, theirs, "banana", "Banana"))["kcal"] == 89

    # A food someone adds is theirs alone
    secret = (
        await client.post(
            f"{PREFIX}/eating/foods",
            json={"name": "Bakin ajvar", "kcal": 120, "protein": 2, "carbs": 10, "fat": 8},
            headers=mine,
        )
    ).json()
    assert secret["mine"] is True

    found = (
        await client.get(f"{PREFIX}/eating/foods", params={"q": "ajvar"}, headers=theirs)
    ).json()
    assert found == []
    only_mine = (
        await client.get(f"{PREFIX}/eating/foods", params={"mine": True}, headers=mine)
    ).json()
    assert [food["name"] for food in only_mine] == ["Bakin ajvar"]
    assert (await client.get(f"{PREFIX}/eating/settings", headers=theirs)).json()["foods"] == len(
        FOODS
    )

    # Nor can it be edited, parsed, eaten or cooked with by guessing its id
    stranger_edit = await client.patch(
        f"{PREFIX}/eating/foods/{secret['id']}", json={"kcal": 1}, headers=theirs
    )
    assert stranger_edit.status_code == 404
    parsed = (
        await client.post(f"{PREFIX}/eating/parse", json={"text": "100g ajvar"}, headers=theirs)
    ).json()
    assert parsed["items"] == [] and parsed["unknown"] == ["100g ajvar"]

    meal = (
        await client.post(
            f"{PREFIX}/eating/meals",
            json={"day": DAY, "items": [{"food_id": secret["id"], "quantity": 100, "unit": "g"}]},
            headers=theirs,
        )
    ).json()
    assert meal["items"][0]["food_id"] is None and meal["kcal"] == 0

    recipe = (
        await client.post(
            f"{PREFIX}/eating/recipes",
            json={
                "title": "Pinđur",
                "items": [{"food_id": secret["id"], "quantity": 100, "unit": "g"}],
            },
            headers=theirs,
        )
    ).json()
    assert recipe["items"][0]["food_id"] is None and recipe["kcal"] == 0

    swapped = await client.patch(
        f"{PREFIX}/eating/meals/{meal['id']}/items/{meal['items'][0]['id']}",
        json={"food_id": secret["id"]},
        headers=theirs,
    )
    assert swapped.status_code == 404


async def test_a_barcode_becomes_a_food_of_ones_own(client: AsyncClient, seeds, monkeypatch):
    from app.services import food_lookup

    headers = await auth_headers(client, email="eat5@example.com")

    monkeypatch.setattr(food_lookup, "read_barcode", lambda content: "3856789012345")

    async def fake_lookup(barcode: str):
        return food_lookup.Product(
            barcode=barcode,
            name="Pro Whey Vanilla",
            brand="Ogistra",
            kcal=390,
            protein=76,
            carbs=9,
            fat=6,
            base_unit="g",
            serving_grams=30,
        )

    monkeypatch.setattr(food_lookup, "lookup", fake_lookup)
    photo = {"photo": ("tub.jpg", b"\xff\xd8\xff photo", "image/jpeg")}
    response = await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is True and body["food"]["name"] == "Pro Whey Vanilla"
    assert body["food"]["units"]["piece"] == 30 and body["food"]["mine"] is True

    # The same tub a second time is the food we already have, not a new one
    again = (await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=headers)).json()
    assert again["food"]["id"] == body["food"]["id"]

    # Someone else scanning the same tub gets a copy of their own
    other = await auth_headers(client, email="eat5b@example.com")
    theirs = (await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=other)).json()
    assert theirs["found"] is True and theirs["food"]["id"] != body["food"]["id"]
    assert theirs["food"]["mine"] is True


async def test_a_barcode_miss_says_so(client: AsyncClient, seeds, monkeypatch):
    from app.services import food_lookup

    headers = await auth_headers(client, email="miss@example.com")
    photo = {"photo": ("tub.jpg", b"\xff\xd8\xff photo", "image/jpeg")}

    monkeypatch.setattr(food_lookup, "read_barcode", lambda content: None)
    blurred = (
        await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=headers)
    ).json()
    assert blurred["found"] is False and blurred["barcode"] is None and blurred["message"]

    monkeypatch.setattr(food_lookup, "read_barcode", lambda content: "4000000000000")

    async def nothing(barcode: str):
        return None

    monkeypatch.setattr(food_lookup, "lookup", nothing)
    unknown = (
        await client.post(f"{PREFIX}/eating/foods/scan", files=photo, headers=headers)
    ).json()
    assert unknown["found"] is False and unknown["barcode"] == "4000000000000"


async def test_the_diary_prints_day_by_day(client: AsyncClient, seeds):
    headers = await auth_headers(client, email="eat6@example.com")
    parsed = (
        await client.post(
            f"{PREFIX}/eating/parse", json={"text": "100g ovsenih, 1 banana"}, headers=headers
        )
    ).json()
    for day, title in ((DAY, "Doručak"), ("2026-09-22", "Ručak")):
        await client.post(
            f"{PREFIX}/eating/meals",
            json={
                "day": day,
                "at": "09:00:00",
                "title": title,
                "items": [
                    {"food_id": i["food_id"], "quantity": i["quantity"], "unit": i["unit"]}
                    for i in parsed["items"]
                ],
            },
            headers=headers,
        )

    pdf = await client.get(
        f"{PREFIX}/eating/export",
        params={"from": DAY, "to": "2026-09-22", "format": "pdf"},
        headers=headers,
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")
    assert "Nutrijurnal_2026-09-21_2026-09-22.pdf" in pdf.headers["content-disposition"]

    csv_file = await client.get(
        f"{PREFIX}/eating/export",
        params={"from": DAY, "to": "2026-09-22", "format": "csv"},
        headers=headers,
    )
    assert csv_file.status_code == 200
    rows = csv_file.content.decode("utf-8-sig").splitlines()
    assert rows[0].startswith("Day;Time;Meal;Item")
    assert sum(1 for row in rows if "DAY TOTAL" in row) == 2

    # Someone else's export of the same days is empty
    other = await auth_headers(client, email="eat6b@example.com")
    theirs = await client.get(
        f"{PREFIX}/eating/export",
        params={"from": DAY, "to": "2026-09-22", "format": "csv"},
        headers=other,
    )
    assert "DAY TOTAL" not in theirs.content.decode("utf-8-sig")

    backwards = await client.get(
        f"{PREFIX}/eating/export", params={"from": "2026-09-22", "to": DAY}, headers=headers
    )
    assert backwards.status_code == 400


async def test_the_diary_is_each_persons_own(client: AsyncClient, seeds):
    mine = await auth_headers(client, email="eat7@example.com")
    theirs = await auth_headers(client, email="eat8@example.com")
    banana = await _food(client, mine, "banana", "Banana")
    meal = (
        await client.post(
            f"{PREFIX}/eating/meals",
            json={
                "day": DAY,
                "title": "Mine",
                "items": [{"food_id": banana["id"], "quantity": 1, "unit": "piece"}],
            },
            headers=mine,
        )
    ).json()
    recipe = (
        await client.post(f"{PREFIX}/eating/recipes", json={"title": "Moje"}, headers=mine)
    ).json()
    item = meal["items"][0]["id"]

    assert (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=theirs)).json()["meals"] == []
    days = await client.get(
        f"{PREFIX}/eating/days", params={"from": DAY, "to": DAY}, headers=theirs
    )
    assert days.json() == []
    assert (await client.get(f"{PREFIX}/eating/recipes", headers=theirs)).json() == []

    # Every way in to someone else's meal or recipe is a plain 404
    meal_path = f"{PREFIX}/eating/meals/{meal['id']}"
    recipe_path = f"{PREFIX}/eating/recipes/{recipe['id']}"
    attempts = [
        client.patch(meal_path, json={"title": "Theirs"}, headers=theirs),
        client.post(f"{meal_path}/items", json={"label": "x"}, headers=theirs),
        client.patch(f"{meal_path}/items/{item}", json={"quantity": 9}, headers=theirs),
        client.delete(f"{meal_path}/items/{item}", headers=theirs),
        client.get(f"{meal_path}/voice", headers=theirs),
        client.delete(meal_path, headers=theirs),
        client.get(recipe_path, headers=theirs),
        client.patch(recipe_path, json={"title": "Theirs"}, headers=theirs),
        client.delete(recipe_path, headers=theirs),
        client.post(
            f"{PREFIX}/eating/meals/from-recipe",
            json={"recipe_id": recipe["id"], "day": DAY},
            headers=theirs,
        ),
    ]
    for attempt in attempts:
        assert (await attempt).status_code == 404

    # And none of it moved anything
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=mine)).json()
    assert day["meals"][0]["title"] == "Mine" and day["meals"][0]["items"][0]["quantity"] == 1
    assert (await client.delete(meal_path, headers=mine)).status_code == 204


async def test_a_batch_is_counted_in_pieces_and_eaten_by_the_piece(client: AsyncClient, seeds):
    """Twelve muffins from one bowl: the recipe holds the whole batch, and
    eating five of them puts five twelfths of it in the diary."""
    headers = await auth_headers(client, email="eat9@example.com")
    egg = await _food(client, headers, "jaje", "Jaje")

    made = await client.post(
        f"{PREFIX}/eating/recipes",
        json={
            "title": "Mafini",
            "servings": 12,
            "serving_unit": "piece",
            "items": [{"food_id": egg["id"], "quantity": 8, "unit": "piece"}],
        },
        headers=headers,
    )
    assert made.status_code == 201, made.text
    recipe = made.json()
    assert recipe["serving_unit"] == "piece"
    assert recipe["items"][0]["grams"] == 440  # 8 × 55 g

    meal = await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe["id"], "day": DAY, "servings": 5},
        headers=headers,
    )
    assert meal.status_code == 201, meal.text
    assert meal.json()["items"][0]["grams"] == round(440 * 5 / 12, 1)

    back = await client.patch(
        f"{PREFIX}/eating/recipes/{recipe['id']}",
        json={"serving_unit": "serving"},
        headers=headers,
    )
    assert back.status_code == 200, back.text
    assert back.json()["serving_unit"] == "serving"

    wrong = await client.patch(
        f"{PREFIX}/eating/recipes/{recipe['id']}",
        json={"serving_unit": "muffin"},
        headers=headers,
    )
    assert wrong.status_code == 422


async def test_food_search_matches_whole_words_not_any_substring(client: AsyncClient, seeds):
    """ "sir" must not find "krompir". The search feeds a picker, and the first
    row is what an ingredient silently becomes."""
    headers = await auth_headers(client, email="eat10@example.com")

    for name in ("Krompir", "Beli sir"):
        made = await client.post(
            f"{PREFIX}/eating/foods",
            json={"name": name, "kcal": 100, "protein": 2, "carbs": 20, "fat": 1},
            headers=headers,
        )
        assert made.status_code == 201, made.text

    for query in ("sir", "sira"):
        found = [
            food["name"]
            for food in (
                await client.get(f"{PREFIX}/eating/foods", params={"q": query}, headers=headers)
            ).json()
        ]
        assert "Beli sir" in found, query
        assert "Krompir" not in found, query


async def test_a_meal_can_be_spoken_and_the_recording_is_kept(client: AsyncClient, seeds):
    """Hands busy, so it is said out loud: the words land in the note and the
    recording stays, because the phone in a kitchen may not dictate."""
    headers = await auth_headers(client, email="eat11@example.com")

    meal = (
        await client.post(
            f"{PREFIX}/eating/meals",
            json={"day": DAY, "title": "Ručak", "note": "pola pileta i pirinač"},
            headers=headers,
        )
    ).json()
    assert meal["has_voice"] is False

    said = await client.post(
        f"{PREFIX}/eating/meals/{meal['id']}/voice",
        files={"file": ("note.webm", b"RIFFfake-audio-bytes", "audio/webm;codecs=opus")},
        data={"seconds": "12.4", "transcribed": "true"},
        headers=headers,
    )
    assert said.status_code == 200, said.text
    assert said.json()["has_voice"] is True
    assert said.json()["voice_seconds"] == 12.4
    assert said.json()["voice_transcribed"] is True

    # The listing says a recording exists without ever carrying the bytes
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["meals"][0]["has_voice"] is True
    assert "voice" not in day["meals"][0]

    played = await client.get(f"{PREFIX}/eating/meals/{meal['id']}/voice", headers=headers)
    assert played.status_code == 200
    assert played.content == b"RIFFfake-audio-bytes"
    assert played.headers["content-type"].startswith("audio/webm")

    wrong = await client.post(
        f"{PREFIX}/eating/meals/{meal['id']}/voice",
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=headers,
    )
    assert wrong.status_code == 415

    dropped = await client.delete(f"{PREFIX}/eating/meals/{meal['id']}/voice", headers=headers)
    assert dropped.status_code == 200
    assert dropped.json()["has_voice"] is False
    assert dropped.json()["note"] == "pola pileta i pirinač"
    assert (
        await client.get(f"{PREFIX}/eating/meals/{meal['id']}/voice", headers=headers)
    ).status_code == 404


async def test_a_dish_known_only_by_its_numbers_can_be_written_and_eaten(
    client: AsyncClient, seeds
):
    """A label or a plan prints "Kcal: 742, P: 24g, C: 98g, F: 25g" and no
    ingredients. That is enough to keep the dish and enough to eat it."""
    headers = await auth_headers(client, email="eat12@example.com")

    made = await client.post(
        f"{PREFIX}/eating/recipes",
        json={
            "title": "Četiri palačinke sa čokoladom",
            "stated": {"kcal": 742, "protein": 24, "carbs": 98, "fat": 25},
        },
        headers=headers,
    )
    assert made.status_code == 201, made.text
    recipe = made.json()
    assert recipe["stated"]["kcal"] == 742
    assert recipe["items"] == []
    assert recipe["kcal"] == 0

    eaten = await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe["id"], "day": DAY, "servings": 1},
        headers=headers,
    )
    assert eaten.status_code == 201, eaten.text
    assert eaten.json()["kcal"] == 742
    assert eaten.json()["protein"] == 24
    assert eaten.json()["items"][0]["label"] == "Četiri palačinke sa čokoladom"

    half = await client.post(
        f"{PREFIX}/eating/meals/from-recipe",
        json={"recipe_id": recipe["id"], "day": DAY, "servings": 0.5},
        headers=headers,
    )
    assert half.json()["kcal"] == 371

    fixed = await client.patch(
        f"{PREFIX}/eating/recipes/{recipe['id']}",
        json={"stated": {"kcal": 700, "protein": 24, "carbs": 90, "fat": 25}},
        headers=headers,
    )
    assert fixed.json()["stated"]["kcal"] == 700


def test_a_word_spelled_exactly_beats_a_shared_stem():
    """Five-letter stems make "secer" and "secerac" the same word, so sugar
    and sweetcorn tied and the table's order decided it — "8 g secera" came
    back as corn. The spelling the food itself uses wins the tie."""
    foods = [
        nutrition.FoodLike(
            id="corn",
            name="Kukuruz šećerac",
            search_key=nutrition.key_of("Kukuruz šećerac", ["šećerac", "kukuruz"]),
            units=None,
            kcal=86,
        ),
        nutrition.FoodLike(
            id="sugar",
            name="Šećer",
            search_key=nutrition.key_of("Šećer", ["šećera", "kristal šećer"]),
            units=None,
            kcal=400,
        ),
    ]

    for written in ("šećer", "šećera", "secer"):
        assert nutrition.match_food(written, foods).id == "sugar", written
        assert nutrition.match_food(written, list(reversed(foods))).id == "sugar", written

    assert nutrition.match_food("kukuruz šećerac", foods).id == "corn"


def test_the_shipped_seed_is_coherent():
    """The staples that ride in the image: unique keys, sane numbers, units
    the diary understands. A broken seed file should fail here, not on
    someone's phone."""
    foods = json.loads(eating_seed.FOODS_FILE.read_text(encoding="utf-8"))
    assert len(foods) > 120

    by_key = {food["key"]: food for food in foods}
    assert len(by_key) == len(foods), "two foods share a key"
    for food in foods:
        assert food["name"] and 0 <= food["kcal"] <= 1000, food["key"]
        assert food["base_unit"] in ("g", "ml"), food["key"]
        assert set(food.get("units") or {}) <= {
            "piece",
            "scoop",
            "tbsp",
            "tsp",
            "cup",
            "handful",
            "pinch",
            "slice",
            "ml",
        }, food["key"]


async def test_seeding_twice_changes_nothing(client: AsyncClient, session: AsyncSession):
    """No monkeypatched file: the seed that ships is the one that runs, and it
    is keyed, so the second run updates in place instead of duplicating."""
    first = await eating_seed.seed_foods(session)
    await session.commit()
    second = await eating_seed.seed_foods(session)
    await session.commit()

    count = await session.scalar(select(func.count()).select_from(Food))
    assert first == second == count > 120
    shared = await session.scalar(
        select(func.count()).select_from(Food).where(Food.user_id.is_(None))
    )
    assert shared == count

    # And the parser finds the staples by their Serbian names
    headers = await auth_headers(client, email="seed@example.com")
    parsed = (
        await client.post(
            f"{PREFIX}/eating/parse",
            json={"text": "200g pilećih grudi, 70g pirinča, 1 kašika maslinovog ulja"},
            headers=headers,
        )
    ).json()
    assert parsed["unknown"] == []
    assert len(parsed["items"]) == 3 and parsed["items"][0]["grams"] == 200
