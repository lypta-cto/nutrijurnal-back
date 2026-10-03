"""
A barcode into a food. Open Food Facts is a free, open database with most
European products in it, so a tub of whey is read once from its barcode and
never typed in again. No key, no account; a miss simply means the person
fills in the four numbers from the label themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

API = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
FIELDS = "product_name,product_name_sr,brands,nutriments,serving_size,quantity,product_quantity"
# Open Food Facts asks every client to name itself, so a misbehaving one can be told apart
AGENT = "Nutrijurnal/0.1 (food diary)"

# A serving is mostly written with a count first — "1 portion (60 g)",
# "2 keksa (25 g)" — so the weight is the number that carries a unit
SERVING_WEIGHT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:g|gr|grams?|ml)\b", re.IGNORECASE)
BARE_NUMBER = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*")
# "500 ml", "1 L", "1,5 l", "75 cl" — sold by volume, so measured in millilitres
BY_VOLUME = re.compile(r"\d\s*(?:ml|cl|dl|l|lit(?:re|er)s?)\b", re.IGNORECASE)


@dataclass
class Product:
    barcode: str
    name: str
    brand: str | None
    kcal: float
    protein: float
    carbs: float
    fat: float
    base_unit: str
    serving_grams: float | None


def read_barcode(content: bytes) -> str | None:
    """The digits under the bars, from a photo. zxing knows EAN-13 and UPC,
    which is what a packet in a European shop carries."""
    from io import BytesIO

    import zxingcpp
    from PIL import Image, ImageOps

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:  # pragma: no cover — HEIC simply won't open
        pass

    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(content)))
    except Exception:  # noqa: BLE001 — anything unreadable is "no barcode"
        return None
    image = image.convert("L")
    tried = [image]
    for side in (2000, 1200):
        if max(image.size) > side:
            smaller = image.copy()
            smaller.thumbnail((side, side))
            tried.append(smaller)
    for candidate in tried:
        for result in zxingcpp.read_barcodes(candidate):
            digits = "".join(ch for ch in result.text if ch.isdigit())
            if 8 <= len(digits) <= 14:
                return digits
    return None


def serving_grams(serving: str) -> float | None:
    """The grams (or millilitres) of one serving as Open Food Facts writes it."""
    match = SERVING_WEIGHT.search(serving) or BARE_NUMBER.fullmatch(serving)
    if match is None:
        return None
    grams = float(match.group(1).replace(",", "."))
    return grams if grams > 0 else None


def _number(nutriments: dict, *names: str) -> float | None:
    for name in names:
        value = nutriments.get(name)
        if isinstance(value, int | float):
            return float(value)
    return None


async def lookup(barcode: str) -> Product | None:
    """What the world knows about this barcode, per 100 g."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": AGENT}) as client:
            response = await client.get(API.format(barcode=barcode), params={"fields": FIELDS})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if body.get("status") != 1:
        return None
    product = body.get("product") or {}
    nutriments = product.get("nutriments") or {}

    kcal = _number(nutriments, "energy-kcal_100g")
    if kcal is None:
        kilojoules = _number(nutriments, "energy_100g", "energy-kj_100g")
        kcal = round(kilojoules / 4.184, 1) if kilojoules else None
    if kcal is None:
        return None
    name = (product.get("product_name_sr") or product.get("product_name") or "").strip()
    if not name:
        return None
    return Product(
        barcode=barcode,
        name=name[:120],
        brand=((product.get("brands") or "").split(",")[0].strip() or None),
        kcal=kcal,
        protein=_number(nutriments, "proteins_100g") or 0.0,
        carbs=_number(nutriments, "carbohydrates_100g") or 0.0,
        fat=_number(nutriments, "fat_100g") or 0.0,
        base_unit="ml" if BY_VOLUME.search(product.get("quantity") or "") else "g",
        serving_grams=serving_grams(product.get("serving_size") or ""),
    )
