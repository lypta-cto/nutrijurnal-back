"""
A barcode into a food. Open Food Facts is a free, open database with most
European products in it, so a tub of whey is read once from its barcode and
never typed in again. No key, no account; a miss simply means the person
fills in the four numbers from the label themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
FIELDS = "product_name,product_name_sr,brands,nutriments,serving_size,quantity,product_quantity"
# Open Food Facts asks every client to name itself, so a misbehaving one can be told apart
AGENT = "Nutrijurnal/0.1 (food diary)"


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
    serving = product.get("serving_size") or ""
    grams = None
    digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in serving).split()
    if digits:
        try:
            grams = float(digits[0])
        except ValueError:
            grams = None
    return Product(
        barcode=barcode,
        name=name[:120],
        brand=((product.get("brands") or "").split(",")[0].strip() or None),
        kcal=kcal,
        protein=_number(nutriments, "proteins_100g") or 0.0,
        carbs=_number(nutriments, "carbohydrates_100g") or 0.0,
        fat=_number(nutriments, "fat_100g") or 0.0,
        base_unit="ml" if "ml" in (product.get("quantity") or "").lower() else "g",
        serving_grams=grams,
    )
