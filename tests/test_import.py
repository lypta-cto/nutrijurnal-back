"""Bringing a diary in: a backup or a diary CSV lands in the account with the
numbers it was eaten at, twice-imported adds nothing twice, and a file can
never reach into someone else's foods."""

import json

from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers, find_food, meal, own_food

IMPORT = f"{PREFIX}/auth/me/import"
DAY = "2026-09-25"


async def _import(client: AsyncClient, headers: dict, name: str, body: bytes):
    kind = "application/json" if name.endswith(".json") else "text/csv"
    return await client.post(IMPORT, files={"file": (name, body, kind)}, headers=headers)


async def _day(client: AsyncClient, headers: dict, day: str = DAY) -> dict:
    response = await client.get(f"{PREFIX}/eating/days/{day}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_diary_csv_moves_to_another_account_with_its_numbers(client: AsyncClient, seeds):
    """The CSV the export writes — the same columns the CTO app writes — is
    read back meal by meal, totals and all."""
    ana = await auth_headers(client, "ana@example.com")
    egg = await find_food(client, ana, "jaje", "Jaje")
    await meal(
        client,
        ana,
        DAY,
        {"food_id": egg["id"], "quantity": 2, "unit": "piece"},
        title="Doručak",
        at="08:40",
    )
    await meal(
        client,
        ana,
        DAY,
        {
            "label": "Torta u kancelariji",
            "macros": {"kcal": 350, "protein": 5, "carbs": 40, "fat": 18},
        },
        title="Užina",
        at="16:00",
    )
    exported = await client.get(
        f"{PREFIX}/eating/export", params={"from": DAY, "to": DAY, "format": "csv"}, headers=ana
    )
    assert exported.status_code == 200

    bob = await auth_headers(client, "bob@example.com")
    first = await _import(client, bob, "diary.csv", exported.content)

    assert first.status_code == 200, first.text
    assert first.json()["meals"] == 2
    theirs, ours = await _day(client, ana), await _day(client, bob)
    assert [m["title"] for m in ours["meals"]] == ["Doručak", "Užina"]
    for name in ("kcal", "protein", "carbs", "fat"):
        assert abs(ours["totals"][name] - theirs["totals"][name]) < 0.6, name

    # The same file again adds nothing
    again = await _import(client, bob, "diary.csv", exported.content)
    assert again.json()["meals"] == 0
    assert again.json()["skipped"] == 2
    assert len((await _day(client, bob))["meals"]) == 2


async def test_a_backup_restores_recipes_and_meals_and_finds_shared_foods_by_key(
    client: AsyncClient, seeds
):
    document = {
        "app": "Nutrijurnal",
        "foods": [
            {
                "id": "old-cheese",
                "name": "Mladi sir",
                "kcal": 180,
                "protein": 13,
                "carbs": 2.5,
                "fat": 13,
            }
        ],
        "recipes": [
            {
                "title": "Proteinski mafini",
                "servings": 12,
                "serving_unit": "piece",
                "items": [
                    {
                        "food_key": "jaje",
                        "label": "Jaje",
                        "quantity": 8,
                        "unit": "piece",
                        "grams": 440,
                    },
                    {
                        "food_id": "old-cheese",
                        "label": "Mladi sir",
                        "quantity": 220,
                        "unit": "g",
                        "grams": 220,
                    },
                ],
            },
            {
                "title": "Pilav",
                "servings": 4,
                "stated": {"kcal": 2770, "protein": 87, "carbs": 387, "fat": 97},
                "items": [],
            },
        ],
        "meals": [
            {
                "day": DAY,
                "at": "13:00:00",
                "title": "Ručak",
                "items": [
                    {
                        "food_key": "jaje",
                        "label": "Jaje",
                        "quantity": 100,
                        "unit": "g",
                        "grams": 100,
                        "kcal100": 143,
                        "protein100": 12.6,
                        "carbs100": 0.7,
                        "fat100": 9.5,
                    }
                ],
            }
        ],
        "water": [{"day": DAY, "ml": 250}, {"day": DAY, "ml": 250}],
        "weight": [{"day": DAY, "kg": 84.2}],
    }
    luka = await auth_headers(client, "luka@example.com")
    response = await _import(client, luka, "backup.json", json.dumps(document).encode())

    assert response.status_code == 200, response.text
    assert response.json() | {"warnings": []} == {
        "foods": 1,
        "recipes": 2,
        "meals": 1,
        "water": 2,
        "weight": 1,
        "skipped": 0,
        "warnings": [],
    }
    recipes = (await client.get(f"{PREFIX}/eating/recipes", headers=luka)).json()
    muffins = next(recipe for recipe in recipes if recipe["title"] == "Proteinski mafini")
    # The egg is the shared one (found by its key), the cheese the new own food
    assert muffins["kcal"] > 0
    detail = (await client.get(f"{PREFIX}/eating/recipes/{muffins['id']}", headers=luka)).json()
    assert all(item["food_id"] for item in detail["items"])
    pilav = next(recipe for recipe in recipes if recipe["title"] == "Pilav")
    assert pilav["stated"]["kcal"] == 2770
    assert (await _day(client, luka))["totals"]["kcal"] == 143

    again = await _import(client, luka, "backup.json", json.dumps(document).encode())
    assert again.json()["recipes"] == again.json()["meals"] == again.json()["foods"] == 0
    assert len((await client.get(f"{PREFIX}/eating/recipes", headers=luka)).json()) == 2


async def test_a_backup_never_reaches_into_someone_elses_foods(client: AsyncClient, seeds):
    """A file names foods by id; only shared foods and the importer's own are
    ever linked — another person's private food is just a label."""
    ana = await auth_headers(client, "ana@example.com")
    secret = await own_food(client, ana, name="Anin hleb")
    bob = await auth_headers(client, "bob@example.com")
    document = {
        "recipes": [
            {"title": "Sendvič", "items": [{"food_id": secret["id"], "label": "Hleb", "grams": 80}]}
        ]
    }

    response = await _import(client, bob, "backup.json", json.dumps(document).encode())

    assert response.status_code == 200
    recipe = (await client.get(f"{PREFIX}/eating/recipes", headers=bob)).json()[0]
    detail = (await client.get(f"{PREFIX}/eating/recipes/{recipe['id']}", headers=bob)).json()
    assert detail["items"][0]["food_id"] is None


async def test_a_round_trip_through_the_backup_keeps_everything(client: AsyncClient, seeds):
    ana = await auth_headers(client, "ana@example.com")
    egg = await find_food(client, ana, "jaje", "Jaje")
    await meal(
        client, ana, DAY, {"food_id": egg["id"], "quantity": 3, "unit": "piece"}, title="Omlet"
    )
    backup = await client.get(f"{PREFIX}/auth/me/export", headers=ana)
    assert backup.status_code == 200
    items = backup.json()["meals"][0]["items"]
    assert items[0]["food_key"] == "jaje"

    bob = await auth_headers(client, "bob@example.com")
    response = await _import(client, bob, "backup.json", backup.content)

    assert response.json()["meals"] == 1
    restored = (await _day(client, bob))["meals"][0]["items"][0]
    assert restored["food_id"] == egg["id"]


async def test_what_is_not_a_diary_is_refused_plainly(client: AsyncClient):
    luka = await auth_headers(client, "luka@example.com")

    for name, body in (
        ("notes.txt", b"just some words"),
        ("broken.json", b"{not json"),
        ("empty.json", b'{"app": "Nutrijurnal"}'),
        ("other.csv", b"a;b;c\n1;2;3\n"),
    ):
        response = await _import(client, luka, name, body)
        assert response.status_code == 422, (name, response.text)
        assert response.json()["detail"]

    huge = await _import(client, luka, "huge.csv", b"x" * (10 * 1024 * 1024 + 1))
    assert huge.status_code == 413
