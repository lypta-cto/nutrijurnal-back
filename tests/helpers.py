import io
import zlib

from httpx import AsyncClient
from PIL import Image

from app.core.config import settings

PREFIX = settings.API_V1_PREFIX
PASSWORD = "supersecret1"

# Four staples with numbers easy to add up by hand; the `seeds` fixture loads them
SMALL_PANTRY = [
    {
        "key": "ovsene-pahuljice",
        "name": "Ovsene pahuljice",
        "name_en": "Oats",
        "kcal": 379,
        "protein": 13.2,
        "carbs": 67.7,
        "fat": 6.5,
        "base_unit": "g",
        "units": {"tbsp": 10},
        "aliases": ["ovsenih pahuljica", "ovsene"],
    },
    {
        "key": "whey",
        "name": "Whey protein",
        "name_en": "Whey",
        "kcal": 380,
        "protein": 78,
        "carbs": 8,
        "fat": 5,
        "base_unit": "g",
        "units": {"scoop": 30},
        "aliases": ["whey proteina", "merica wheya"],
    },
    {
        "key": "banana",
        "name": "Banana",
        "name_en": "Banana",
        "kcal": 89,
        "protein": 1.1,
        "carbs": 22.8,
        "fat": 0.3,
        "base_unit": "g",
        "units": {"piece": 120},
        "aliases": ["banane"],
    },
    {
        "key": "jaje",
        "name": "Jaje",
        "name_en": "Egg",
        "kcal": 155,
        "protein": 13,
        "carbs": 1.1,
        "fat": 11,
        "base_unit": "g",
        "units": {"piece": 55},
        "aliases": ["jaja", "jajeta"],
    },
]


async def sign_up(client: AsyncClient, email: str, full_name: str = "Test User") -> dict:
    """Signs a new person up and hands back the whole answer: token and user."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def auth_headers(client: AsyncClient, email: str, full_name: str = "Test User") -> dict:
    """Signs a new person up and hands back the header their requests carry."""
    body = await sign_up(client, email, full_name)
    return bearer(body["access_token"])


async def find_food(client: AsyncClient, headers: dict, q: str, name: str) -> dict:
    """One food out of a search, by its exact name — fails loudly when absent."""
    found = (await client.get(f"{PREFIX}/eating/foods", params={"q": q}, headers=headers)).json()
    matches = [food for food in found if food["name"] == name]
    assert matches, f"{name!r} not among {[food['name'] for food in found]}"
    return matches[0]


async def own_food(client: AsyncClient, headers: dict, **fields) -> dict:
    """A food of one's own, with sensible numbers unless told otherwise."""
    body = {"name": "Moj hleb", "kcal": 250, "protein": 8, "carbs": 50, "fat": 2, **fields}
    response = await client.post(f"{PREFIX}/eating/foods", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def meal(client: AsyncClient, headers: dict, day: str, *items: dict, **fields) -> dict:
    """A meal on `day` holding `items` (ItemWrite bodies)."""
    body = {"day": day, "title": "Meal", "items": list(items), **fields}
    response = await client.post(f"{PREFIX}/eating/meals", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims a vast canvas — the shape of a
    decompression bomb, without ever allocating one in the test."""
    buffer = io.BytesIO()
    Image.new("L", (8, 8)).save(buffer, format="PNG")
    data = bytearray(buffer.getvalue())
    # IHDR follows the 8-byte signature: length, "IHDR", width, height, …, CRC
    data[16:20] = width.to_bytes(4, "big")
    data[20:24] = height.to_bytes(4, "big")
    data[29:33] = zlib.crc32(bytes(data[12:29])).to_bytes(4, "big")
    return bytes(data)
