"""
A demo diary: a throwaway account with two weeks of eating already in it,
so anyone can see what a lived-in Nutrijurnal looks like in one tap.

Everything here is generic — everyday plates built from the shared pantry,
water, a slowly falling weight, two simple recipes of the demo's own. Nothing
comes from any real person's diary or meal plan. The numbers vary from day
to day (a seeded random per account), so the charts look like a life and
not like a test fixture.
"""

from __future__ import annotations

import random
import secrets
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.models.body import WaterEntry, WeightEntry
from app.models.eating import FavouriteFood, Food, Meal, MealItem, Recipe, RecipeItem
from app.models.user import User
from app.schemas.goals import GoalProfile
from app.services import goals, nutrition

DAYS = 14

# (title, [(shared food key, quantity, unit)]) — everyday plates, nothing more
PLATES: dict[str, list[tuple[str, list[tuple[str, float, str]]]]] = {
    "breakfast": [
        (
            "Ovsena kaša",
            [
                ("ovsene-pahuljice", 60, "g"),
                ("mleko-16", 200, "ml"),
                ("banana", 1, "piece"),
                ("med", 1, "tsp"),
            ],
        ),
        (
            "Jaja i hleb",
            [("jaje", 2, "piece"), ("integralni-hleb", 2, "slice"), ("paradajz", 1, "piece")],
        ),
        ("Skyr sa voćem", [("skyr", 200, "g"), ("sumsko-voce", 1, "handful"), ("musli", 30, "g")]),
        (
            "Grčki jogurt",
            [
                ("grcki-jogurt", 200, "g"),
                ("borovnica", 1, "handful"),
                ("med", 1, "tsp"),
                ("badem", 15, "g"),
            ],
        ),
    ],
    "lunch": [
        (
            "Piletina sa pirinčem",
            [
                ("pileci-file", 150, "g"),
                ("pirinac", 70, "g"),
                ("salata", 2, "handful"),
                ("maslinovo-ulje", 1, "tbsp"),
            ],
        ),
        (
            "Testenina bolonjeze",
            [("testenina", 90, "g"), ("junece-mleveno", 120, "g"), ("paradajz-sos", 150, "g")],
        ),
        ("Losos i krompir", [("losos", 140, "g"), ("krompir", 200, "g"), ("brokoli", 150, "g")]),
        (
            "Ćuretina i povrće",
            [
                ("cureci-file", 150, "g"),
                ("batat", 180, "g"),
                ("tikvice", 150, "g"),
                ("maslinovo-ulje", 1, "tsp"),
            ],
        ),
    ],
    "dinner": [
        (
            "Salata sa tunjevinom",
            [
                ("tunjevina", 1, "piece"),
                ("salata", 2, "handful"),
                ("krastavac", 1, "piece"),
                ("paradajz", 1, "piece"),
                ("maslinovo-ulje", 1, "tbsp"),
            ],
        ),
        (
            "Omlet",
            [
                ("jaje", 3, "piece"),
                ("spanac", 1, "handful"),
                ("beli-sir", 1, "slice"),
                ("integralni-hleb", 1, "slice"),
            ],
        ),
        (
            "Tost sa šunkom",
            [("tost-hleb", 3, "slice"), ("sunka", 3, "slice"), ("gauda", 2, "slice")],
        ),
        (
            "Pileći batak i batat",
            [("pileci-batak", 2, "piece"), ("batat", 1, "piece"), ("paprika", 1, "piece")],
        ),
    ],
    "snack": [
        ("Jabuka", [("jabuka", 1, "piece")]),
        ("Bademi", [("badem", 1, "handful")]),
        ("Protein bar", [("protein-bar", 1, "piece")]),
        ("Kafa sa mlekom", [("kafa", 1, "cup"), ("mleko-16", 50, "ml")]),
        ("Crna čokolada", [("crna-cokolada", 20, "g")]),
        ("Banana i kikiriki puter", [("banana", 1, "piece"), ("kikiriki-puter", 1, "tbsp")]),
    ],
}

RECIPES: list[tuple[str, list[tuple[str, float, str]]]] = [
    (
        "Overnight oats",
        [
            ("ovsene-pahuljice", 50, "g"),
            ("mleko-16", 150, "ml"),
            ("cia", 1, "tbsp"),
            ("banana", 1, "piece"),
        ],
    ),
    (
        "Činija piletine i pirinča",
        [
            ("pileci-file", 150, "g"),
            ("pirinac", 75, "g"),
            ("brokoli", 100, "g"),
            ("maslinovo-ulje", 1, "tbsp"),
        ],
    ),
]

FAVOURITES = ("ovsene-pahuljice", "banana", "grcki-jogurt")

# The hours each slot is usually eaten in, as (earliest, latest) minutes
HOURS = {
    "breakfast": (7 * 60 + 15, 8 * 60 + 45),
    "lunch": (12 * 60 + 30, 14 * 60),
    "dinner": (19 * 60, 20 * 60 + 30),
    "snack": (10 * 60 + 15, 16 * 60 + 45),
}


def _like(food: Food) -> nutrition.FoodLike:
    return nutrition.FoodLike(
        id=food.id,
        name=food.name,
        search_key=food.search_key,
        units=food.units,
        base_unit=food.base_unit,
        kcal=food.kcal,
        protein=food.protein,
        carbs=food.carbs,
        fat=food.fat,
    )


def _clock(rng: random.Random, slot: str) -> time:
    earliest, latest = HOURS[slot]
    minutes = rng.randrange(earliest, latest, 5)
    return time(minutes // 60, minutes % 60)


def _item(food: Food, quantity: float, unit: str, position: int) -> MealItem:
    return MealItem(
        position=position,
        food_id=food.id,
        label=food.name,
        quantity=quantity,
        unit=unit,
        grams=nutrition.grams_for(_like(food), quantity, unit),
        kcal100=food.kcal,
        protein100=food.protein,
        carbs100=food.carbs,
        fat100=food.fat,
    )


def _plate(
    rng: random.Random, pantry: dict[str, Food], user: User, day: date, slot: str
) -> Meal | None:
    title, lines = rng.choice(PLATES[slot])
    items = []
    for key, quantity, unit in lines:
        food = pantry.get(key)
        if food is None:
            continue
        # Nobody weighs the same 60 g every morning
        if unit in ("g", "ml"):
            quantity = max(5, round(quantity * rng.uniform(0.95, 1.3) / 5) * 5)
        items.append(_item(food, quantity, unit, len(items)))
    if not items:
        return None
    return Meal(user_id=user.id, day=day, at=_clock(rng, slot), title=title, slot=slot, items=items)


async def create_demo(session, today: date | None = None) -> User:
    """A new demo account, already onboarded, with its two weeks in it."""
    today = today or date.today()
    token = secrets.token_hex(4)
    rng = random.Random(token)
    user = User(
        email=f"demo-{token}@demo.nutrijurnal.app",
        full_name="Demo",
        is_demo=True,
        demo_expires_at=datetime.now(UTC) + timedelta(days=settings.DEMO_TTL_DAYS),
        onboarded_at=datetime.now(UTC),
        water_goal_ml=2000,
        water_glass_ml=250,
    )
    session.add(user)
    await session.flush()

    # A slow, believable loss: a quarter of a kilo a week, with the scale's
    # usual day-to-day wobble
    start_weight = round(rng.uniform(66, 74), 1)
    weights = []
    for offset in range(DAYS - 1, -1, -2):
        day = today - timedelta(days=offset)
        trend = start_weight - 0.035 * (DAYS - 1 - offset)
        kg = round(trend + rng.uniform(-0.2, 0.2), 1)
        weights.append(WeightEntry(user_id=user.id, day=day, kg=kg))
    session.add_all(weights)

    profile = GoalProfile(
        sex="female",
        birth_year=today.year - rng.randint(28, 40),
        height_cm=rng.choice([163, 166, 168, 171]),
        weight_kg=weights[-1].kg,
        activity="moderate",
        goal="lose",
        pace=0.25,
    )
    goals.keep_profile(user, profile)
    plan = goals.estimate(profile, today)
    user.target_kcal, user.target_protein = plan.kcal, plan.protein
    user.target_carbs, user.target_fat = plan.carbs, plan.fat

    keys = {key for plates in PLATES.values() for _, lines in plates for key, _, _ in lines}
    keys |= {key for _, lines in RECIPES for key, _, _ in lines} | set(FAVOURITES)
    pantry = {
        food.key: food
        for food in (
            await session.execute(select(Food).where(Food.key.in_(keys), Food.user_id.is_(None)))
        ).scalars()
    }

    meals = []
    water = []
    for offset in range(DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        is_today = offset == 0
        # Today is still going: breakfast and a coffee so far, the rest to add
        slots = ["breakfast", "snack"] if is_today else ["breakfast", "lunch", "dinner", "snack"]
        if not is_today and rng.random() < 0.65:
            slots.append("snack")
        for slot in slots:
            meal = _plate(rng, pantry, user, day, slot)
            if meal is not None:
                meals.append(meal)
        glasses = 3 if is_today else rng.randint(5, 9)
        for glass in range(glasses):
            ml = 500 if rng.random() < 0.2 else 250
            # Spread over the waking day, so the glasses read in order
            stamp = datetime.combine(day, time(8 + glass * 13 // glasses, 0), tzinfo=UTC)
            water.append(WaterEntry(user_id=user.id, day=day, ml=ml, created_at=stamp))
    session.add_all(meals)
    session.add_all(water)

    for title, lines in RECIPES:
        items = [
            RecipeItem(
                position=index,
                food_id=pantry[key].id,
                label=pantry[key].name,
                quantity=quantity,
                unit=unit,
                grams=nutrition.grams_for(_like(pantry[key]), quantity, unit),
            )
            for index, (key, quantity, unit) in enumerate(lines)
            if key in pantry
        ]
        session.add(Recipe(user_id=user.id, title=title, servings=1, items=items))

    for key in FAVOURITES:
        if key in pantry:
            session.add(FavouriteFood(user_id=user.id, food_id=pantry[key].id))

    await session.flush()
    return user
