"""The live scanner's lookup: digits read by the phone's camera (or typed off
the packet) into a food, asking Open Food Facts at most once per person."""

import pytest
from httpx import AsyncClient

from app.services import food_lookup
from tests.helpers import PREFIX, auth_headers

CODE = "3017620422003"

PRODUCT = food_lookup.Product(
    barcode=CODE,
    name="Lešnik krem",
    brand="Neko",
    kcal=539,
    protein=6.3,
    carbs=57.5,
    fat=30.9,
    base_unit="g",
    serving_grams=15,
)


@pytest.fixture
def off(monkeypatch) -> list[str]:
    """Open Food Facts, answering from memory and remembering who asked."""
    asked: list[str] = []

    async def lookup(barcode: str):
        asked.append(barcode)
        return PRODUCT if barcode == CODE else None

    monkeypatch.setattr(food_lookup, "lookup", lookup)
    return asked


async def test_a_scanned_code_becomes_the_persons_own_food_once(client: AsyncClient, off):
    headers = await auth_headers(client, "scanner@example.com")

    first = await client.get(f"{PREFIX}/eating/foods/barcode/{CODE}", headers=headers)

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["found"] is True and body["barcode"] == CODE
    assert body["food"]["name"] == "Lešnik krem"
    assert body["food"]["mine"] is True
    assert body["food"]["units"] == {"piece": 15}

    # The second scan is answered from the person's own pantry
    second = await client.get(f"{PREFIX}/eating/foods/barcode/{CODE}", headers=headers)
    assert second.json()["food"]["id"] == body["food"]["id"]
    assert off == [CODE]


async def test_an_unknown_code_says_so_and_keeps_the_digits(client: AsyncClient, off):
    headers = await auth_headers(client, "unknown@example.com")

    response = await client.get(f"{PREFIX}/eating/foods/barcode/8600000000017", headers=headers)

    body = response.json()
    assert body["found"] is False and body["food"] is None
    assert body["barcode"] == "8600000000017"
    assert "label" in body["message"]


async def test_one_persons_scan_is_not_anothers_food(client: AsyncClient, off):
    first = await auth_headers(client, "first-scan@example.com")
    second = await auth_headers(client, "second-scan@example.com")

    mine = (await client.get(f"{PREFIX}/eating/foods/barcode/{CODE}", headers=first)).json()
    theirs = (await client.get(f"{PREFIX}/eating/foods/barcode/{CODE}", headers=second)).json()

    assert mine["food"]["id"] != theirs["food"]["id"]
    assert off == [CODE, CODE]


async def test_a_food_added_by_hand_with_its_barcode_is_found_without_asking(
    client: AsyncClient, off
):
    headers = await auth_headers(client, "label@example.com")
    created = await client.post(
        f"{PREFIX}/eating/foods",
        json={"name": "Domaći ajvar", "kcal": 120, "barcode": "8600000000017"},
        headers=headers,
    )

    found = await client.get(f"{PREFIX}/eating/foods/barcode/8600000000017", headers=headers)

    assert found.json()["food"]["id"] == created.json()["id"]
    assert off == []


async def test_only_digits_of_a_retail_length_are_looked_up(client: AsyncClient, off):
    headers = await auth_headers(client, "digits@example.com")

    for code in ("12345", "abc4567890123", "123456789012345"):
        response = await client.get(f"{PREFIX}/eating/foods/barcode/{code}", headers=headers)
        assert response.status_code == 422, code
    assert off == []

    anonymous = await client.get(f"{PREFIX}/eating/foods/barcode/{CODE}")
    assert anonymous.status_code == 401
