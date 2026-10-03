"""The food library: the shared staples, one's own additions, finding them
by any of their names, and putting them away."""

import pytest
from httpx import AsyncClient

from tests.helpers import PREFIX, SMALL_PANTRY, auth_headers, meal, own_food


@pytest.fixture
async def me(client: AsyncClient, seeds) -> dict:
    return await auth_headers(client, email="foods@example.com")


async def _names(client: AsyncClient, headers: dict, **params) -> list[str]:
    response = await client.get(f"{PREFIX}/eating/foods", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [food["name"] for food in response.json()]


async def test_a_food_of_ones_own_keeps_only_units_the_diary_knows(client: AsyncClient, me):
    food = await own_food(
        client,
        me,
        name="  Proteinski puding  ",
        brand="Z Bregov",
        base_unit="litre",
        units={"piece": 200, "bucket": 5000, "tbsp": 0, "scoop": -3},
    )

    assert (food["name"], food["brand"], food["base_unit"]) == (
        "Proteinski puding",
        "Z Bregov",
        "g",
    )
    assert food["units"] == {"piece": 200}
    assert (food["source"], food["mine"], food["archived"], food["barcode"]) == (
        "manual",
        True,
        False,
        None,
    )

    bare = await own_food(client, me, name="Samo grami", base_unit="ml")
    assert bare["units"] == {} and bare["base_unit"] == "ml"
    labelled = await own_food(client, me, name="Sa barkodom", barcode="40084015")
    assert labelled["source"] == "barcode"


async def test_what_a_food_accepts_is_bounded(client: AsyncClient, me):
    base = {"name": "x", "kcal": 100}
    for case, body in {
        "no name": {"kcal": 100},
        "empty name": {**base, "name": ""},
        "long name": {**base, "name": "x" * 121},
        "no kcal": {"name": "x"},
        "over 1000 kcal per 100 g": {**base, "kcal": 1001},
        "negative kcal": {**base, "kcal": -1},
        "over 100 g protein per 100 g": {**base, "protein": 101},
        "negative fat": {**base, "fat": -0.1},
        "long barcode": {**base, "barcode": "1" * 33},
        "too many aliases": {**base, "aliases": [f"alias {n}" for n in range(21)]},
        "long alias": {**base, "aliases": ["x" * 121]},
    }.items():
        response = await client.post(f"{PREFIX}/eating/foods", json=body, headers=me)
        assert response.status_code == 422, case


async def test_a_portion_weighs_what_a_portion_can(client: AsyncClient, me):
    """A 'piece' of 1e308 g made a meal of it infinite, and every number of
    that day was answered as null to the diary, Progress and the export."""
    path = f"{PREFIX}/eating/foods"
    for units in ({"slice": 1e308}, {"piece": 5000.5}):
        response = await client.post(
            path, json={"name": "Hleb", "kcal": 250, "units": units}, headers=me
        )
        assert response.status_code == 422, units
    too_many = {f"unit {n}": 10 for n in range(21)}
    response = await client.post(
        path, json={"name": "Hleb", "kcal": 250, "units": too_many}, headers=me
    )
    assert response.status_code == 422

    family_pizza = await own_food(client, me, name="Pica", units={"piece": 5000})
    assert family_pizza["units"] == {"piece": 5000}
    patched = await client.patch(
        f"{path}/{family_pizza['id']}", json={"units": {"slice": 1e308}}, headers=me
    )
    assert patched.status_code == 422


async def test_the_library_lists_the_shared_foods_and_ones_own(client: AsyncClient, me):
    await own_food(client, me, name="Ajvar")

    assert await _names(client, me) == sorted(
        ["Ajvar", *(food["name"] for food in SMALL_PANTRY)], key=str.lower
    )
    assert await _names(client, me, mine=True) == ["Ajvar"]
    assert len(await _names(client, me, limit=2)) == 2

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["foods"] == len(SMALL_PANTRY) + 1


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("Ovsene", "Ovsene pahuljice"),
        ("ovsenih pahuljica", "Ovsene pahuljice"),
        # The English name reaches a shared food too
        ("oats", "Ovsene pahuljice"),
        ("EGG", "Jaje"),
        ("jajeta", "Jaje"),
    ],
)
async def test_a_food_is_found_by_any_of_its_names(client: AsyncClient, me, query, first):
    assert (await _names(client, me, q=query))[0] == first


async def test_search_ignores_diacritics_and_finds_brands(client: AsyncClient, me):
    await own_food(client, me, name="Pileća šunka", brand="Carnex")

    assert await _names(client, me, q="pileca sunka") == ["Pileća šunka"]
    assert await _names(client, me, q="ŠUNKA") == ["Pileća šunka"]
    assert await _names(client, me, q="carnex") == ["Pileća šunka"]
    assert await _names(client, me, q="nothing like it") == []


async def test_more_words_matched_rank_first_then_shorter_names(client: AsyncClient, me):
    for name in ("Sir gauda dimljeni", "Beli sir", "Sir"):
        await own_food(client, me, name=name)

    assert await _names(client, me, q="beli sir") == ["Beli sir", "Sir", "Sir gauda dimljeni"]


async def test_editing_ones_own_food_changes_how_it_is_found(client: AsyncClient, me):
    food = await own_food(client, me, name="Kolač", brand="Mamin", units={"slice": 80})
    path = f"{PREFIX}/eating/foods/{food['id']}"

    renamed = await client.patch(path, json={"name": "Torta", "brand": None}, headers=me)
    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["brand"]) == ("Torta", None)
    assert await _names(client, me, q="torta") == ["Torta"]
    assert await _names(client, me, q="kolac") == []
    assert await _names(client, me, q="mamin") == []

    # Units are replaced whole and filtered like on the way in; nulls and
    # unknown base units leave things as they were
    patched = (
        await client.patch(
            path,
            json={"units": {"piece": 120, "bowl": 300}, "kcal": None, "base_unit": "cup"},
            headers=me,
        )
    ).json()
    assert (patched["units"], patched["kcal"], patched["base_unit"]) == (
        {"piece": 120},
        250,
        "g",
    )
    assert (await client.patch(path, json={"units": {}}, headers=me)).json()["units"] == {}
    assert (await client.patch(path, json={"kcal": 1001}, headers=me)).status_code == 422


async def test_an_archived_food_leaves_the_library_but_not_the_diary(client: AsyncClient, me):
    food = await own_food(client, me, name="Stari hleb", kcal=250)
    eaten = await meal(client, me, "2026-09-21", {"food_id": food["id"], "quantity": 100})
    path = f"{PREFIX}/eating/foods/{food['id']}"

    archived = await client.patch(path, json={"archived": True}, headers=me)
    assert archived.json()["archived"] is True
    assert "Stari hleb" not in await _names(client, me)
    assert await _names(client, me, q="hleb") == []
    assert await _names(client, me, mine=True) == []
    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["foods"] == len(SMALL_PANTRY)

    day = (await client.get(f"{PREFIX}/eating/days/2026-09-21", headers=me)).json()
    assert [m["kcal"] for m in day["meals"]] == [eaten["kcal"]] == [250]

    # And it can be brought back
    restored = await client.patch(path, json={"archived": False}, headers=me)
    assert restored.json()["archived"] is False
    assert await _names(client, me, mine=True) == ["Stari hleb"]


async def test_an_unknown_food_is_a_404(client: AsyncClient, me):
    response = await client.patch(
        f"{PREFIX}/eating/foods/00000000-0000-4000-8000-000000000000",
        json={"kcal": 1},
        headers=me,
    )
    assert response.status_code == 404
    assert (
        await client.patch(f"{PREFIX}/eating/foods/not-a-uuid", json={"kcal": 1}, headers=me)
    ).status_code == 422


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("chicken", "Pileći file"),
        ("oats", "Ovsene pahuljice"),
        ("chickpeas", "Leblebija"),
    ],
)
async def test_a_whole_english_word_beats_one_that_only_shares_its_stem(
    client: AsyncClient, pantry, query, first
):
    """ "chicken" and "chickpeas" share five letters, "oats" and "oat bran"
    three — the food actually named what was typed comes first."""
    headers = await auth_headers(client, email="ranking@example.com")

    assert (await _names(client, headers, q=query))[0] == first
