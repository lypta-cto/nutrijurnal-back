"""
Filling the shared pantry: the staples every diary starts with.

Seeded foods have no owner — nobody may edit them, so a correction to oats
is a change to `foods_seed.json`, shipped to everyone at once. They are keyed,
so running this again updates what changed and adds what is new instead of
piling up duplicates. People's own recipes are never seeded: everyone builds
their own book.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.models.eating import Food
from app.services.nutrition import key_of

DATA = Path(__file__).resolve().parent.parent / "data"
FOODS_FILE = DATA / "foods_seed.json"


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


async def seed_foods(session) -> int:
    """The shared table of foods. Returns how many rows were touched."""
    rows = _load(FOODS_FILE)
    if not rows:
        return 0
    existing = {
        food.key: food
        for food in (await session.execute(select(Food).where(Food.key.is_not(None)))).scalars()
    }
    touched = 0
    for row in rows:
        key = (row.get("key") or "").strip()
        if not key or not row.get("name"):
            continue
        food = existing.get(key)
        if food is None:
            food = Food(key=key, source="seed")
            session.add(food)
            existing[key] = food
        aliases = [str(alias) for alias in (row.get("aliases") or [])]
        # A seeded food is shared and read-only, whatever the row was before
        food.user_id = None
        food.archived = False
        food.name = row["name"]
        food.name_en = row.get("name_en")
        food.aliases = aliases
        food.search_key = key_of(row["name"], [*aliases, row.get("name_en") or ""])
        food.kcal = float(row.get("kcal") or 0)
        food.protein = float(row.get("protein") or 0)
        food.carbs = float(row.get("carbs") or 0)
        food.fat = float(row.get("fat") or 0)
        food.base_unit = row.get("base_unit") or "g"
        food.units = {
            unit: float(grams)
            for unit, grams in (row.get("units") or {}).items()
            if isinstance(grams, int | float) and grams > 0
        } or None
        touched += 1
    await session.flush()
    return touched
