"""Bringing a diary in: a Nutrijurnal backup (JSON) or a diary CSV.

The CSV is the one the diary export writes — and the CTO Productivity App's,
whose eating diary this app grew out of and which writes the same columns —
so a diary kept there moves here with the numbers it was eaten at.

Importing the same file twice adds nothing twice: a recipe is matched by its
title, a food by its name and brand, a meal by its day, time, name and what
was on the plate, a weighing and a day's water by their day.
"""

import csv
import io
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models.body import WaterEntry, WeightEntry
from app.models.eating import UNITS, Food, Meal, MealItem, Recipe, RecipeItem
from app.services import nutrition
from app.services.slots import slot_of

# A diary of years is a few thousand rows; anything far past it is not one
MAX_ROWS = 50_000
CSV_HEADER = ("day", "time", "meal", "item", "quantity", "unit", "grams")


class NotAnImport(ValueError):
    """The file is neither a Nutrijurnal backup nor a diary CSV."""


@dataclass
class ImportResult:
    foods: int = 0
    recipes: int = 0
    meals: int = 0
    water: int = 0
    weight: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


async def import_file(session: AsyncSession, user: User, raw: bytes) -> ImportResult:
    text = raw.decode("utf-8-sig", errors="replace").lstrip()
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise NotAnImport("That JSON file could not be read.") from error
        if not isinstance(document, dict):
            raise NotAnImport("That JSON file is not a Nutrijurnal backup.")
        return await _import_document(session, user, document)
    first = text.splitlines()[0] if text else ""
    if tuple(cell.strip().lower() for cell in first.split(";")[:7]) == CSV_HEADER:
        return await _import_csv(session, user, text)
    raise NotAnImport(
        "This file is neither a Nutrijurnal backup (.json) nor a diary export (.csv)."
    )


# --- What the file names, found here ----------------------------------------


class _Pantry:
    """Every food an imported line may point at: the shared ones by their key,
    the person's own by name, and the backup's own foods by the id they had
    where they came from."""

    def __init__(self, shared: dict[str, Food], visible: dict[uuid.UUID, Food], own: dict):
        self.shared = shared
        self.visible = visible
        self.own = own
        self.from_backup: dict[str, Food] = {}

    def find(self, entry: dict) -> Food | None:
        key = entry.get("food_key")
        if isinstance(key, str) and key in self.shared:
            return self.shared[key]
        old = str(entry.get("food_id") or "")
        if old in self.from_backup:
            return self.from_backup[old]
        try:
            return self.visible.get(uuid.UUID(old))
        except ValueError:
            return None


async def _pantry(session: AsyncSession, user: User) -> _Pantry:
    foods = (
        await session.execute(
            select(Food).where((Food.user_id.is_(None)) | (Food.user_id == user.id))
        )
    ).scalars()
    shared, visible, own = {}, {}, {}
    for food in foods:
        visible[food.id] = food
        if food.user_id is None and food.key:
            shared[food.key] = food
        elif food.user_id == user.id:
            own[_food_identity(food.name, food.brand)] = food
    return _Pantry(shared, visible, own)


def _food_identity(name: str | None, brand: str | None) -> tuple[str, str]:
    return ((name or "").strip().lower(), (brand or "").strip().lower())


# --- A backup ----------------------------------------------------------------


async def _import_document(session: AsyncSession, user: User, document: dict) -> ImportResult:
    sections = ("foods", "recipes", "meals", "water", "weight")
    if not any(isinstance(document.get(name), list) for name in sections):
        raise NotAnImport("That JSON file has no foods, recipes or meals in it.")

    result = ImportResult()
    pantry = await _pantry(session, user)

    for entry in _list(document, "foods"):
        name = str(entry.get("name") or "").strip()[:120]
        if not name:
            continue
        identity = _food_identity(name, entry.get("brand"))
        food = pantry.own.get(identity)
        if food is None:
            units = entry.get("units") if isinstance(entry.get("units"), dict) else {}
            food = Food(
                # Set here, not at the flush: recipes and meals below point at it
                id=uuid.uuid4(),
                user_id=user.id,
                name=name,
                brand=(str(entry["brand"])[:120] if entry.get("brand") else None),
                search_key=nutrition.key_of(name, [entry.get("brand") or ""]),
                kcal=_number(entry.get("kcal")),
                protein=_number(entry.get("protein")),
                carbs=_number(entry.get("carbs")),
                fat=_number(entry.get("fat")),
                base_unit="ml" if entry.get("base_unit") == "ml" else "g",
                units={
                    unit: float(grams)
                    for unit, grams in units.items()
                    if unit in UNITS and _number(grams) > 0
                }
                or None,
                barcode=(str(entry["barcode"])[:32] if entry.get("barcode") else None),
                source="manual",
            )
            session.add(food)
            pantry.own[identity] = food
            pantry.visible[food.id] = food
            result.foods += 1
        if entry.get("id"):
            pantry.from_backup[str(entry["id"])] = food
    await session.flush()

    titles = {
        _recipe_identity(row.title, row.subtitle)
        for row in (
            await session.execute(select(Recipe).where(Recipe.user_id == user.id))
        ).scalars()
    }
    for entry in _list(document, "recipes"):
        title = str(entry.get("title") or "").strip()[:160]
        if not title:
            continue
        identity = _recipe_identity(title, entry.get("subtitle"))
        if identity in titles:
            result.skipped += 1
            continue
        titles.add(identity)
        stated = entry.get("stated")
        recipe = Recipe(
            user_id=user.id,
            title=title,
            subtitle=(str(entry["subtitle"])[:160] if entry.get("subtitle") else None),
            servings=max(_number(entry.get("servings")), 1),
            serving_unit="piece" if entry.get("serving_unit") == "piece" else "serving",
            minutes=int(entry["minutes"]) if _number(entry.get("minutes")) > 0 else None,
            steps=[str(step) for step in entry.get("steps") or []] or None,
            note=entry.get("note"),
            stated=(
                {name: _number(stated.get(name)) for name in ("kcal", "protein", "carbs", "fat")}
                if isinstance(stated, dict)
                else None
            ),
        )
        recipe.items = [
            RecipeItem(
                position=position,
                food_id=(food.id if (food := pantry.find(item)) else None),
                label=str(item.get("label") or (food.name if food else "Ingredient"))[:160],
                quantity=_number(item.get("quantity")),
                unit=_unit(item.get("unit")),
                grams=_number(item.get("grams")),
                optional=bool(item.get("optional")),
            )
            for position, item in enumerate(_list(entry, "items"))
        ]
        session.add(recipe)
        result.recipes += 1

    plates = []
    for entry in _list(document, "meals"):
        day = _day(entry.get("day"))
        if day is None:
            continue
        items = []
        for item in _list(entry, "items"):
            food = pantry.find(item)
            items.append(
                {
                    "food_id": food.id if food else None,
                    "label": str(item.get("label") or (food.name if food else "Food"))[:160],
                    "quantity": _number(item.get("quantity")),
                    "unit": _unit(item.get("unit"), allow_serving=True),
                    "grams": _number(item.get("grams")),
                    "kcal100": _number(item.get("kcal100")),
                    "protein100": _number(item.get("protein100")),
                    "carbs100": _number(item.get("carbs100")),
                    "fat100": _number(item.get("fat100")),
                }
            )
        plates.append(
            {
                "day": day,
                "at": _time(entry.get("at")),
                "title": str(entry.get("title") or "Meal")[:160],
                "slot": entry.get("slot"),
                "recipe_title": entry.get("recipe_title"),
                "servings": max(_number(entry.get("servings")), 0) or 1,
                "note": entry.get("note"),
                "items": items,
            }
        )
    await _add_meals(session, user, plates, result)

    await _add_water(session, user, _list(document, "water"), result)
    await _add_weight(session, user, _list(document, "weight"), result)
    await session.flush()
    return result


def _recipe_identity(title: str, subtitle: str | None) -> tuple[str, str]:
    return (title.strip().lower(), (subtitle or "").strip().lower())


# --- A diary CSV ---------------------------------------------------------------


async def _import_csv(session: AsyncSession, user: User, text: str) -> ImportResult:
    reader = csv.reader(io.StringIO(text), delimiter=";")
    next(reader, None)
    meals: OrderedDict[tuple, dict] = OrderedDict()
    result = ImportResult()
    for count, row in enumerate(reader, start=1):
        if count > MAX_ROWS:
            result.warnings.append(f"Only the first {MAX_ROWS} rows were read.")
            break
        if len(row) < 11 or not row[0].strip():
            continue
        day = _day(row[0])
        title = row[2].strip()
        # The export closes every day with a total; the diary adds those up itself
        if day is None or title.upper() == "DAY TOTAL":
            continue
        at = _time(row[1])
        meal = meals.setdefault(
            (day, at, title),
            {"day": day, "at": at, "title": title[:160] or "Meal", "items": []},
        )
        grams = _number(row[6])
        totals = [_number(cell) for cell in (row[10], row[7], row[8], row[9])]
        if grams > 0:
            per100 = [value / grams * 100 for value in totals]
            quantity, unit = _number(row[4]), _unit(row[5], allow_serving=True)
        else:
            # Numbers with no weight behind them — a dish known by its totals:
            # one serving, the way the diary stores a quick kcal entry
            per100, quantity, unit, grams = totals, 1.0, "serving", 100.0
        meal["items"].append(
            {
                "food_id": None,
                "label": (row[3].strip() or title or "Food")[:160],
                "quantity": quantity,
                "unit": unit,
                "grams": grams,
                "kcal100": round(per100[0], 3),
                "protein100": round(per100[1], 3),
                "carbs100": round(per100[2], 3),
                "fat100": round(per100[3], 3),
            }
        )
    if not meals and not result.warnings:
        raise NotAnImport("That CSV has no meals in it.")
    await _add_meals(session, user, list(meals.values()), result)
    await session.flush()
    return result


# --- Shared by both ------------------------------------------------------------


async def _add_meals(session: AsyncSession, user: User, plates: list[dict], result) -> None:
    if not plates:
        return
    latest = datetime.now(UTC).date() + timedelta(days=1)
    days = {plate["day"] for plate in plates}
    existing = (
        await session.execute(select(Meal).where(Meal.user_id == user.id, Meal.day.in_(days)))
    ).scalars()
    seen = {_plate_print(meal.day, meal.at, meal.title, meal.items) for meal in existing}

    for plate in plates:
        if plate["day"] > latest:
            result.skipped += 1
            continue
        fingerprint = _plate_print(plate["day"], plate["at"], plate["title"], plate["items"])
        if fingerprint in seen:
            result.skipped += 1
            continue
        seen.add(fingerprint)
        meal = Meal(
            user_id=user.id,
            day=plate["day"],
            at=plate["at"],
            title=plate["title"],
            slot=slot_of(plate.get("slot"), plate["at"], plate["title"]),
            recipe_title=plate.get("recipe_title"),
            servings=plate.get("servings") or 1,
            note=plate.get("note"),
        )
        meal.items = [MealItem(position=index, **item) for index, item in enumerate(plate["items"])]
        session.add(meal)
        result.meals += 1


def _plate_print(day: date, at: time | None, title: str, items) -> tuple:
    def of(item) -> tuple:
        get = item.get if isinstance(item, dict) else (lambda name: getattr(item, name))
        grams = _number(get("grams"))
        return (
            str(get("label")).strip().lower(),
            round(grams, 1),
            round(_number(get("kcal100")) * grams / 100),
        )

    stamp = at.replace(second=0, microsecond=0) if at else None
    return (day, stamp, title.strip().lower(), tuple(sorted(of(item) for item in items)))


async def _add_water(session: AsyncSession, user: User, entries: list[dict], result) -> None:
    wanted = [
        (day, int(_number(entry.get("ml")))) for entry in entries if (day := _day(entry.get("day")))
    ]
    if not wanted:
        return
    had = set(
        (
            await session.execute(
                select(WaterEntry.day).where(
                    WaterEntry.user_id == user.id, WaterEntry.day.in_({day for day, _ in wanted})
                )
            )
        ).scalars()
    )
    for day, ml in wanted:
        # A day that already has glasses keeps them: water carries no time to
        # tell an imported glass from one drunk since
        if day in had or not 0 < ml <= 5000:
            result.skipped += 1
            continue
        session.add(WaterEntry(user_id=user.id, day=day, ml=ml))
        result.water += 1


async def _add_weight(session: AsyncSession, user: User, entries: list[dict], result) -> None:
    wanted = {day: _number(entry.get("kg")) for entry in entries if (day := _day(entry.get("day")))}
    if not wanted:
        return
    had = set(
        (
            await session.execute(
                select(WeightEntry.day).where(
                    WeightEntry.user_id == user.id, WeightEntry.day.in_(wanted.keys())
                )
            )
        ).scalars()
    )
    for day, kg in wanted.items():
        if day in had or not 20 <= kg <= 400:
            result.skipped += 1
            continue
        session.add(WeightEntry(user_id=user.id, day=day, kg=kg))
        result.weight += 1


def _list(container: dict, name: str) -> list[dict]:
    value = container.get(name)
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _number(value) -> float:
    try:
        number = float(str(value).replace(",", ".")) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0
    # A pasted file can say anything; nothing in a diary is negative or astronomic
    return number if 0 <= number < 1_000_000 else 0.0


def _unit(value, allow_serving: bool = False) -> str:
    unit = str(value or "").strip().lower()
    if unit in UNITS or (allow_serving and unit == "serving"):
        return unit
    return "g"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _time(value) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text[:8] if len(text) >= 8 else text[:5])
    except ValueError:
        return None
