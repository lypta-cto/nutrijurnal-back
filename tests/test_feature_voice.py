"""A meal said out loud, in Serbian or English: the slot it names is read off,
the words around the food are dropped, and the rest becomes a draft."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import eating_seed, nutrition
from app.services.slots import take_slot
from tests.helpers import PREFIX, auth_headers


@pytest.fixture
async def shipped(session: AsyncSession) -> int:
    """The shared foods that actually ship — the wording is the point here."""
    touched = await eating_seed.seed_foods(session)
    await session.commit()
    return touched


@pytest.mark.parametrize(
    ("said", "slot", "food"),
    [
        (
            "dodaj 200 g piletine i 100 g pirinča za ručak",
            "lunch",
            "200 g piletine i 100 g pirinča",
        ),
        ("200g chicken and rice for lunch", "lunch", "200g chicken and rice"),
        ("For breakfast I had two eggs and a banana", "breakfast", "two eggs and a banana"),
        ("Večera: pasulj", "dinner", "pasulj"),
        ("pojeo sam jabuku na užini", "snack", "jabuku"),
        ("Doručak - 2 jaja i hleb", "breakfast", "2 jaja i hleb"),
        ("at dinner a slice of pizza", "dinner", "a slice of pizza"),
        ("50g ovsenih, 1 merica whey", None, "50g ovsenih, 1 merica whey"),
    ],
)
def test_the_slot_is_read_off_the_sentence(said: str, slot: str | None, food: str):
    assert take_slot(said) == (slot, food)


@pytest.mark.parametrize(
    ("written", "quantity", "unit", "name"),
    [
        ("two eggs", 2, "piece", "eggs"),
        ("half a banana", 0.5, "piece", "banana"),
        ("100g of rice", 100, "g", "rice"),
        ("2 tablespoons of peanut butter", 2, "tbsp", "peanut butter"),
        ("a glass of milk", 1, "cup", "milk"),
        ("an apple", 1, "piece", "apple"),
    ],
)
def test_amounts_read_in_english_too(written: str, quantity: float, unit: str, name: str):
    amount = nutrition.read_amount(written)

    assert (amount.quantity, amount.unit, amount.name) == (quantity, unit, name)


async def test_a_serbian_sentence_becomes_a_lunch_draft(client: AsyncClient, shipped: int):
    headers = await auth_headers(client, "glas@example.com")

    response = await client.post(
        f"{PREFIX}/eating/parse",
        json={"text": "dodaj 200 g piletine i 100 g pirinča za ručak"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slot"] == "lunch"
    assert [(item["label"], item["grams"]) for item in body["items"]] == [
        ("Pileći file", 200),
        ("Pirinač", 100),
    ]
    assert body["unknown"] == []
    assert body["items"][0]["kcal"] > 0


async def test_an_english_sentence_lands_on_the_plain_staples(client: AsyncClient, shipped: int):
    headers = await auth_headers(client, "voice@example.com")

    response = await client.post(
        f"{PREFIX}/eating/parse",
        json={"text": "For breakfast I had two eggs, 50g oats and a banana"},
        headers=headers,
    )

    body = response.json()
    assert body["slot"] == "breakfast"
    assert [(item["label"], item["quantity"], item["unit"]) for item in body["items"]] == [
        ("Jaje", 2, "piece"),
        ("Ovsene pahuljice", 50, "g"),
        ("Banana", 1, "piece"),
    ]

    rice = await client.post(
        f"{PREFIX}/eating/parse",
        json={"text": "200g chicken and rice for lunch"},
        headers=headers,
    )
    assert [item["label"] for item in rice.json()["items"]] == ["Pileći file", "Pirinač"]
    assert rice.json()["slot"] == "lunch"


async def test_a_sentence_without_a_slot_leaves_it_to_the_page(client: AsyncClient, shipped: int):
    headers = await auth_headers(client, "noslot@example.com")

    response = await client.post(
        f"{PREFIX}/eating/parse", json={"text": "1 banana, nešto čudno"}, headers=headers
    )

    body = response.json()
    assert body["slot"] is None
    assert [item["label"] for item in body["items"]] == ["Banana"]
    assert body["unknown"] == ["nešto čudno"]
