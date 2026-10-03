"""A photo of a barcode into a food of one's own: zxing-cpp reads a real
generated EAN-13 here, and Open Food Facts is the conftest's stand-in — no
test reaches the network."""

import io
import threading

import httpx
import pytest
import zxingcpp
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food
from app.services import food_lookup
from tests.helpers import PREFIX, auth_headers, own_food, png_claiming

# zxing-cpp adds the check digit: 385678901234 → 3856789012348
EAN = "3856789012348"
WHEY = {
    "product_name": "Pro Whey Vanilla",
    "brands": "Ogistra, Ogistra Nutrition",
    "quantity": "900 g",
    "serving_size": "30 g",
    "nutriments": {
        "energy-kcal_100g": 390,
        "proteins_100g": 76,
        "carbohydrates_100g": 9,
        "fat_100g": 6.5,
    },
}


def photo_of(digits: str, image_format: str = "JPEG") -> bytes:
    """A generated EAN-13 on a larger white sheet, the way a phone frames it."""
    symbol = zxingcpp.create_barcode(digits[:12], zxingcpp.BarcodeFormat.EAN13)
    bars = Image.fromarray(symbol.to_image(scale=4)).convert("RGB")
    sheet = Image.new("RGB", (1600, 1200), "white")
    sheet.paste(bars, (500, 450))
    buffer = io.BytesIO()
    sheet.save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.fixture
async def me(client: AsyncClient, seeds) -> dict:
    return await auth_headers(client, email="scanner@example.com")


async def _scan(client: AsyncClient, headers: dict, content: bytes, name: str = "tub.jpg"):
    return await client.post(
        f"{PREFIX}/eating/foods/scan",
        files={"photo": (name, content, "image/jpeg")},
        headers=headers,
    )


async def test_a_photographed_barcode_becomes_a_food_of_ones_own(
    client: AsyncClient, me, open_food_facts
):
    open_food_facts.products[EAN] = WHEY

    response = await _scan(client, me, photo_of(EAN))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["found"], body["barcode"], body["message"]) == (True, EAN, None)
    food = body["food"]
    assert (food["name"], food["brand"], food["source"], food["barcode"]) == (
        "Pro Whey Vanilla",
        "Ogistra",
        "barcode",
        EAN,
    )
    assert (food["kcal"], food["protein"], food["carbs"], food["fat"]) == (390, 76, 9, 6.5)
    assert (food["base_unit"], food["units"], food["mine"]) == ("g", {"piece": 30}, True)

    # Open Food Facts was asked politely, for this code and only the fields we read
    [asked] = open_food_facts.requests
    assert asked.url.path == f"/api/v2/product/{EAN}.json"
    assert "nutriments" in asked.url.params["fields"]
    assert asked.headers["user-agent"].startswith("Nutrijurnal/")

    # It is in the library now, as one's own
    mine = await client.get(f"{PREFIX}/eating/foods", params={"mine": True}, headers=me)
    assert [row["name"] for row in mine.json()] == ["Pro Whey Vanilla"]


async def test_an_iphone_heic_photo_is_read_too(client: AsyncClient, me, open_food_facts):
    open_food_facts.products[EAN] = WHEY

    body = (await _scan(client, me, photo_of(EAN, "HEIF"), "IMG_0001.HEIC")).json()

    assert body["found"] is True and body["barcode"] == EAN


async def test_the_same_barcode_twice_is_the_same_food(client: AsyncClient, me, open_food_facts):
    open_food_facts.products[EAN] = WHEY
    first = (await _scan(client, me, photo_of(EAN))).json()

    second = (await _scan(client, me, photo_of(EAN))).json()

    assert second["food"]["id"] == first["food"]["id"]
    # The second time needs no lookup at all
    assert len(open_food_facts.requests) == 1


async def test_ones_own_copy_wins_over_a_shared_one(
    client: AsyncClient, me, session: AsyncSession, open_food_facts
):
    shared = Food(
        name="Shared whey", search_key="shared whey", kcal=400, barcode=EAN, source="seed"
    )
    session.add(shared)
    await session.commit()

    found = (await _scan(client, me, photo_of(EAN))).json()
    assert found["food"]["name"] == "Shared whey" and found["food"]["mine"] is False
    assert open_food_facts.requests == []

    await own_food(client, me, name="My whey", kcal=380, barcode=EAN)
    found = (await _scan(client, me, photo_of(EAN))).json()
    assert found["food"]["name"] == "My whey" and found["food"]["mine"] is True


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        # The Serbian name, when the product has one, is the one people know
        (
            {
                "product_name": "Plazma",
                "product_name_sr": "Plazma keks",
                "nutriments": {"energy-kcal_100g": 440},
            },
            {"name": "Plazma keks", "kcal": 440, "base_unit": "g", "units": {}},
        ),
        # Kilojoules only: converted, to a tenth
        (
            {"product_name": "Sok", "nutriments": {"energy_100g": 180}},
            {"name": "Sok", "kcal": 43.0, "units": {}},
        ),
        (
            {
                "product_name": "Jogurt",
                "quantity": "500 ml",
                "serving_size": "250 ml",
                "nutriments": {"energy-kj_100g": 250, "proteins_100g": 3},
            },
            {"name": "Jogurt", "kcal": 59.8, "base_unit": "ml", "units": {"piece": 250}},
        ),
    ],
)
async def test_what_open_food_facts_says_is_read_per_100_g(
    client: AsyncClient, me, open_food_facts, product, expected
):
    open_food_facts.products[EAN] = product

    food = (await _scan(client, me, photo_of(EAN))).json()["food"]

    assert {name: food[name] for name in expected} == expected


async def test_a_serving_written_with_a_count_uses_its_grams(
    client: AsyncClient, me, open_food_facts
):
    open_food_facts.products[EAN] = {
        "product_name": "Protein bar",
        "serving_size": "1 portion (60 g)",
        "nutriments": {"energy-kcal_100g": 360},
    }

    food = (await _scan(client, me, photo_of(EAN))).json()["food"]

    assert food["units"] == {"piece": 60}


@pytest.mark.parametrize("quantity", ["1 L", "1,5 l", "330ml", "75 cl"])
async def test_a_drink_sold_by_the_litre_is_measured_in_millilitres(
    client: AsyncClient, me, open_food_facts, quantity
):
    open_food_facts.products[EAN] = {
        "product_name": "Sok od jabuke",
        "quantity": quantity,
        "nutriments": {"energy-kcal_100g": 46},
    }

    food = (await _scan(client, me, photo_of(EAN))).json()["food"]

    assert food["base_unit"] == "ml"


@pytest.mark.parametrize(
    ("serving", "grams"),
    [
        ("30 g", 30),
        ("2 keksa (25 g)", 25),
        ("1 bar (40g)", 40),
        ("12,5 g", 12.5),
        ("250 ml", 250),
        ("60", 60),
        ("1 portion", None),
        ("", None),
        ("0 g", None),
        # A typo on the community's label is not a portion
        ("99999 g", None),
        ("5000 ml", 5000),
    ],
)
def test_a_serving_size_is_read_by_the_number_with_a_unit(serving, grams):
    assert food_lookup.serving_grams(serving) == grams


@pytest.mark.parametrize("quantity", ["500 g", "1 lb", "6 x 25 g", ""])
async def test_a_packet_sold_by_weight_stays_in_grams(
    client: AsyncClient, me, open_food_facts, quantity
):
    open_food_facts.products[EAN] = {
        "product_name": "Keks",
        "quantity": quantity,
        "nutriments": {"energy-kcal_100g": 460},
    }

    food = (await _scan(client, me, photo_of(EAN))).json()["food"]

    assert food["base_unit"] == "g"


@pytest.mark.parametrize(
    "product",
    [
        {"product_name": "No energy", "nutriments": {"proteins_100g": 10}},
        {"product_name": "  ", "nutriments": {"energy-kcal_100g": 100}},
        {"nutriments": {"energy-kcal_100g": 100}},
        # Numbers no food can hold: kilojoules in the kcal field, a slipped decimal
        {"product_name": "Keks", "nutriments": {"energy-kcal_100g": 1900}},
        {"product_name": "Keks", "nutriments": {"energy-kcal_100g": 450, "fat_100g": 250}},
        {"product_name": "Keks", "nutriments": {"energy-kcal_100g": -5}},
    ],
)
async def test_a_product_without_a_name_or_energy_is_a_miss(
    client: AsyncClient, me, open_food_facts, product
):
    open_food_facts.products[EAN] = product

    body = (await _scan(client, me, photo_of(EAN))).json()

    assert (body["found"], body["barcode"], body["food"]) == (False, EAN, None)
    assert body["message"].startswith("Not in Open Food Facts")


async def test_an_unknown_barcode_or_a_dead_service_is_a_miss_not_an_error(
    client: AsyncClient, me, open_food_facts
):
    photo = photo_of(EAN)

    unknown = (await _scan(client, me, photo)).json()
    assert (unknown["found"], unknown["barcode"]) == (False, EAN)

    open_food_facts.status = 503
    down = await _scan(client, me, photo)
    assert down.status_code == 200 and down.json()["found"] is False

    open_food_facts.status = None
    open_food_facts.failure = httpx.ConnectTimeout("no route")
    offline = await _scan(client, me, photo)
    assert offline.status_code == 200 and offline.json()["found"] is False

    # Nothing half-made was saved along the way
    mine = await client.get(f"{PREFIX}/eating/foods", params={"mine": True}, headers=me)
    assert mine.json() == []


def _qr_code() -> bytes:
    symbol = zxingcpp.create_barcode("https://example.com", zxingcpp.BarcodeFormat.QRCode)
    buffer = io.BytesIO()
    Image.fromarray(symbol.to_image(scale=6)).save(buffer, format="PNG")
    return buffer.getvalue()


def _blank() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [_blank(), _qr_code(), b"definitely not an image"],
    ids=["no barcode", "a QR code", "not an image"],
)
async def test_a_photo_without_a_product_barcode_says_so(
    client: AsyncClient, me, open_food_facts, content
):
    response = await _scan(client, me, content)

    assert response.status_code == 200
    body = response.json()
    assert (body["found"], body["barcode"], body["food"]) == (False, None, None)
    assert body["message"] == "No barcode in that photo — try filling the frame."
    assert open_food_facts.requests == []


@pytest.mark.parametrize(("width", "height"), [(9000, 9000), (100_000, 100_000)])
async def test_a_photo_claiming_a_vast_canvas_is_refused_before_decoding(
    client: AsyncClient, me, width, height
):
    response = await _scan(client, me, png_claiming(width, height), name="bomb.png")

    assert response.status_code == 200
    assert response.json()["found"] is False


async def test_a_photo_is_decoded_off_the_event_loop(
    client: AsyncClient, me, open_food_facts, monkeypatch
):
    """Decoding a phone photo takes a while; on the event loop it would hold
    up every other person's request until the bars were found."""
    open_food_facts.products[EAN] = WHEY
    loop_thread = threading.current_thread()
    decoded_on = []
    real_read_barcode = food_lookup.read_barcode

    def spy(content):
        decoded_on.append(threading.current_thread())
        return real_read_barcode(content)

    monkeypatch.setattr(food_lookup, "read_barcode", spy)

    response = await _scan(client, me, photo_of(EAN))

    assert response.json()["found"] is True
    assert decoded_on and decoded_on[0] is not loop_thread


async def test_a_photo_is_bounded_and_required(client: AsyncClient, me):
    too_big = await _scan(client, me, b"\xff" * (12 * 1024 * 1024 + 1))
    assert too_big.status_code == 413
    assert too_big.json()["detail"] == "That photo is over 12 MB"

    missing = await client.post(f"{PREFIX}/eating/foods/scan", headers=me)
    assert missing.status_code == 422

    anonymous = await client.post(
        f"{PREFIX}/eating/foods/scan", files={"photo": ("x.jpg", photo_of(EAN), "image/jpeg")}
    )
    assert anonymous.status_code == 401
