"""
The diary: meals, what went into them, and what they came to.

The app is public, so every route here answers for one person only: meals and
recipes are filtered on the signed-in user, the seeded foods are shared and
read-only, and a food someone types in or scans is theirs alone. Every meal
item carries the food's numbers as they were when it was eaten.
"""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Path, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update

from app.api.deps import CurrentUser, SessionDep
from app.api.routes import body
from app.models.eating import (
    UNITS,
    DeletedMeal,
    FavouriteFood,
    Food,
    Meal,
    MealItem,
    Recipe,
    RecipeItem,
)
from app.schemas.eating import (
    DayCopy,
    DayRead,
    DayTotals,
    FoodPatch,
    FoodPick,
    FoodRead,
    FoodWrite,
    FromRecipe,
    ItemPatch,
    ItemWrite,
    Macros,
    MealCopy,
    MealItemRead,
    MealPatch,
    MealRead,
    MealWrite,
    ParsedItem,
    ParseIn,
    ParseOut,
    QuickFoods,
    RecipePatch,
    RecipeRead,
    RecipeWrite,
    ScanOut,
    SettingsPatch,
    SettingsRead,
)
from app.schemas.goals import GoalEstimate, GoalProfile
from app.services import food_lookup, goals, nutrition, slots

router = APIRouter(prefix="/eating", tags=["eating"])

# `search_key` is "jaje | jaja | jajeta" — words, separated by spaces and pipes
_SPLIT_WORDS = re.compile(r"[\s|]+")


def _words_meet(word: str, want: str) -> bool:
    """Serbian endings move under the word ("sir", "sira", "sirom"), so two
    words meet when one is a prefix of the other from three letters in.
    Matching anywhere inside the text instead made "sir" find "krompir", and
    the top hit is what an ingredient row silently becomes."""
    shared = min(len(word), len(want))
    return shared >= 3 and word[:shared] == want[:shared]


MAX_RANGE_DAYS = 400
MAX_PHOTO_BYTES = 12 * 1024 * 1024


# --- Reading rows out ---------------------------------------------------------


def _as_food_like(food: Food) -> nutrition.FoodLike:
    return nutrition.FoodLike(
        id=food.id,
        name=food.name,
        search_key=food.search_key or nutrition.key_of(food.name, food.aliases),
        units=food.units,
        base_unit=food.base_unit,
        kcal=food.kcal,
        protein=food.protein,
        carbs=food.carbs,
        fat=food.fat,
    )


def _visible_to(user):
    """The shared staples and this person's own — never anyone else's."""
    return or_(Food.user_id.is_(None), Food.user_id == user.id)


async def _foods_for(session, user) -> list[Food]:
    """Everything this person can pick from: the staples plus their own."""
    rows = (
        await session.execute(select(Food).where(_visible_to(user), Food.archived.is_(False)))
    ).scalars()
    return list(rows)


def _food_read(food: Food, user_id: uuid.UUID, favourites: set[uuid.UUID]) -> FoodRead:
    body = FoodRead.model_validate(food)
    body.mine = food.user_id == user_id
    body.favourite = food.id in favourites
    return body


async def _favourite_ids(session, user) -> list[uuid.UUID]:
    """The foods this person starred, the latest first."""
    rows = await session.execute(
        select(FavouriteFood.food_id)
        .where(FavouriteFood.user_id == user.id)
        .order_by(FavouriteFood.created_at.desc())
    )
    return list(rows.scalars())


# How far back "recent" looks: plenty for a dozen distinct foods, cheap to read
RECENT_ITEMS = 300


async def _recent_uses(session, user) -> dict[uuid.UUID, tuple[float, str, date]]:
    """Each food this person ate lately, with the amount they last ate it in.
    Ordered by recency — the dict keeps the order it was filled in."""
    rows = await session.execute(
        select(MealItem.food_id, MealItem.quantity, MealItem.unit, Meal.day)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(Meal.user_id == user.id, MealItem.food_id.is_not(None))
        .order_by(Meal.day.desc(), Meal.created_at.desc(), MealItem.position.desc())
        .limit(RECENT_ITEMS)
    )
    latest: dict[uuid.UUID, tuple[float, str, date]] = {}
    for food_id, quantity, unit, day in rows:
        latest.setdefault(food_id, (quantity, unit, day))
    return latest


def _meal_read(meal: Meal) -> MealRead:
    items = [
        MealItemRead(
            id=item.id,
            food_id=item.food_id,
            label=item.label,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            **item.macros,
        )
        for item in meal.items
    ]
    totals = nutrition.total([item.model_dump() for item in items])
    return MealRead(
        id=meal.id,
        day=meal.day,
        at=meal.at,
        title=meal.title,
        slot=meal.slot,
        recipe_id=meal.recipe_id,
        recipe_title=meal.recipe_title,
        servings=meal.servings,
        note=meal.note,
        # `voice` itself is deferred — asking whether it is there must not load it
        has_voice=meal.voice_type is not None,
        voice_seconds=meal.voice_seconds,
        voice_transcribed=meal.voice_transcribed,
        items=items,
        **totals,
    )


def _recipe_read(recipe: Recipe, foods: dict[uuid.UUID, Food]) -> RecipeRead:
    items, macros = [], []
    for item in recipe.items:
        food = foods.get(item.food_id) if item.food_id else None
        macros.append(nutrition.macros(_as_food_like(food) if food else None, item.grams))
        items.append(
            {
                "id": item.id,
                "food_id": item.food_id,
                "label": item.label,
                "quantity": item.quantity,
                "unit": item.unit,
                "grams": item.grams,
                "optional": item.optional,
            }
        )
    return RecipeRead(
        id=recipe.id,
        title=recipe.title,
        subtitle=recipe.subtitle,
        servings=recipe.servings,
        serving_unit=recipe.serving_unit,
        minutes=recipe.minutes,
        steps=list(recipe.steps or []),
        note=recipe.note,
        items=items,
        stated=Macros(**recipe.stated) if isinstance(recipe.stated, dict) else None,
        **nutrition.total(macros),
    )


def _target(user) -> Macros | None:
    values = (user.target_kcal, user.target_protein, user.target_carbs, user.target_fat)
    if not any(value for value in values):
        return None
    return Macros(
        kcal=user.target_kcal or 0,
        protein=user.target_protein or 0,
        carbs=user.target_carbs or 0,
        fat=user.target_fat or 0,
    )


# --- Settings -----------------------------------------------------------------


async def _counts(session, user) -> tuple[int, int]:
    foods = await session.scalar(
        select(func.count()).select_from(Food).where(_visible_to(user), Food.archived.is_(False))
    )
    recipes = await session.scalar(
        select(func.count()).select_from(Recipe).where(Recipe.user_id == user.id)
    )
    return foods or 0, recipes or 0


@router.get("/settings", response_model=SettingsRead)
async def read_settings(session: SessionDep, user: CurrentUser) -> SettingsRead:
    foods, recipes = await _counts(session, user)
    return SettingsRead(
        target_kcal=user.target_kcal,
        target_protein=user.target_protein,
        target_carbs=user.target_carbs,
        target_fat=user.target_fat,
        onboarded_at=user.onboarded_at,
        profile=goals.profile_of(user),
        water_goal_ml=body.water_goal(user),
        water_glass_ml=body.glass_size(user),
        timezone=user.timezone,
        foods=foods,
        recipes=recipes,
    )


@router.patch("/settings", response_model=SettingsRead)
async def write_settings(
    payload: SettingsPatch, session: SessionDep, user: CurrentUser
) -> SettingsRead:
    fields = payload.model_dump(exclude_unset=True)
    for name in ("target_kcal", "target_protein", "target_carbs", "target_fat"):
        if name in fields:
            setattr(user, name, fields[name])
    for name in ("water_goal_ml", "water_glass_ml", "timezone"):
        if fields.get(name):
            setattr(user, name, fields[name])
    if payload.profile is not None:
        goals.keep_profile(user, payload.profile)
    if fields.get("onboarded") and user.onboarded_at is None:
        user.onboarded_at = datetime.now(UTC)
    await session.flush()
    return await read_settings(session, user)


@router.post("/goals/estimate", response_model=GoalEstimate)
async def estimate_goals(payload: GoalProfile, _: CurrentUser) -> GoalEstimate:
    """What a day should come to for this body and this goal. Nothing is
    saved — the person reads it, adjusts it, and saves the targets they want."""
    return goals.estimate(payload)


# --- Foods --------------------------------------------------------------------


@router.get("/foods", response_model=list[FoodRead])
async def list_foods(
    session: SessionDep,
    user: CurrentUser,
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    mine: bool = Query(default=False, description="Only the foods you added yourself"),
) -> list[FoodRead]:
    foods = await _foods_for(session, user)
    favourites = set(await _favourite_ids(session, user))
    if mine:
        foods = [food for food in foods if food.user_id == user.id]
    query = nutrition.normalize(q)
    if query:
        asked = [word for word in query.split() if len(word) > 2]
        wanted = [nutrition.stem(word) for word in asked]
        # Among equally good matches, what this person stars and eats comes first
        recent = {food_id: rank for rank, food_id in enumerate(await _recent_uses(session, user))}
        scored = []
        for food in foods:
            haystack = f"{food.search_key} {nutrition.normalize(food.brand)}"
            words = _SPLIT_WORDS.split(haystack)
            hits = sum(1 for want in wanted if any(_words_meet(word, want) for word in words))
            if hits or query in haystack:
                # The whole word asked for ("chicken", "oats") beats a word
                # that only shares its stem ("chickpeas", "oat bran")
                whole = sum(1 for word in asked if any(name.startswith(word) for name in words))
                rank = (
                    -hits,
                    -whole,
                    food.id not in favourites,
                    recent.get(food.id, RECENT_ITEMS),
                    len(food.name),
                )
                scored.append((rank, food))
        scored.sort(key=lambda row: row[0])
        foods = [row[1] for row in scored]
    else:
        foods.sort(key=lambda food: food.name.lower())
    return [_food_read(food, user.id, favourites) for food in foods[:limit]]


@router.get("/foods/quick", response_model=QuickFoods)
async def quick_foods(
    session: SessionDep,
    user: CurrentUser,
    limit: int = Query(default=12, ge=1, le=50),
) -> QuickFoods:
    """What adding food opens on: the starred foods, then what was eaten
    lately — each with the amount it was last eaten in."""
    starred = await _favourite_ids(session, user)
    uses = await _recent_uses(session, user)
    favourites = set(starred)
    foods = await _foods_by_id(session, user, favourites | set(list(uses)[: limit * 2]))

    def pick(food: Food) -> FoodPick:
        body = FoodPick(**_food_read(food, user.id, favourites).model_dump())
        if food.id in uses:
            body.last_quantity, body.last_unit, body.last_day = uses[food.id]
        return body

    live = {food_id: food for food_id, food in foods.items() if not food.archived}
    return QuickFoods(
        favourites=[pick(live[food_id]) for food_id in starred if food_id in live],
        recent=[
            pick(live[food_id]) for food_id in uses if food_id in live and food_id not in favourites
        ][:limit],
    )


async def _visible_food(session, user, food_id: uuid.UUID) -> Food:
    food = await session.get(Food, food_id)
    if food is None or (food.user_id is not None and food.user_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such food")
    return food


@router.put("/foods/{food_id}/favourite", response_model=FoodRead)
async def star_food(food_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> FoodRead:
    """Any food you can see may be starred, a shared staple included."""
    food = await _visible_food(session, user, food_id)
    if await session.get(FavouriteFood, (user.id, food.id)) is None:
        session.add(FavouriteFood(user_id=user.id, food_id=food.id))
        await session.flush()
    return _food_read(food, user.id, {food.id})


@router.delete("/foods/{food_id}/favourite", response_model=FoodRead)
async def unstar_food(food_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> FoodRead:
    food = await _visible_food(session, user, food_id)
    starred = await session.get(FavouriteFood, (user.id, food.id))
    if starred is not None:
        await session.delete(starred)
        await session.flush()
    return _food_read(food, user.id, set())


@router.post("/foods", response_model=FoodRead, status_code=status.HTTP_201_CREATED)
async def create_food(payload: FoodWrite, session: SessionDep, user: CurrentUser) -> FoodRead:
    food = Food(
        user_id=user.id,
        name=payload.name.strip(),
        name_en=payload.name_en,
        brand=payload.brand,
        aliases=payload.aliases,
        search_key=nutrition.key_of(payload.name, [*payload.aliases, payload.brand or ""]),
        kcal=payload.kcal,
        protein=payload.protein,
        carbs=payload.carbs,
        fat=payload.fat,
        base_unit=payload.base_unit if payload.base_unit in ("g", "ml") else "g",
        units={unit: grams for unit, grams in payload.units.items() if unit in UNITS and grams > 0}
        or None,
        barcode=payload.barcode,
        source="barcode" if payload.barcode else "manual",
    )
    session.add(food)
    await session.flush()
    return _food_read(food, user.id, set())


@router.patch("/foods/{food_id}", response_model=FoodRead)
async def update_food(
    food_id: uuid.UUID, payload: FoodPatch, session: SessionDep, user: CurrentUser
) -> FoodRead:
    food = await session.get(Food, food_id)
    # Someone else's food is not merely read-only, it does not exist for you
    if food is None or (food.user_id is not None and food.user_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such food")
    if food.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Shared foods can't be edited — add your own version instead",
        )
    fields = payload.model_dump(exclude_unset=True)
    for name, value in fields.items():
        if name == "units" and value is not None:
            food.units = {
                unit: grams for unit, grams in value.items() if unit in UNITS and grams > 0
            } or None
        elif name == "base_unit" and value not in ("g", "ml"):
            continue
        elif value is not None or name in ("brand", "name_en"):
            setattr(food, name, value)
    if {"name", "brand"} & fields.keys():
        food.search_key = nutrition.key_of(food.name, [*(food.aliases or []), food.brand or ""])
    await session.flush()
    return _food_read(food, user.id, set(await _favourite_ids(session, user)))


async def _food_for_barcode(session, user, barcode: str) -> ScanOut:
    """The food behind a barcode: this person's own copy first, then a shared
    one, then Open Food Facts — whose answer is kept as this person's food, so
    the packet is only ever looked up once."""
    existing = (
        await session.execute(
            select(Food)
            .where(Food.barcode == barcode, _visible_to(user), Food.archived.is_(False))
            # Your own copy of a product wins over a shared one
            .order_by(Food.user_id.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        favourites = set(await _favourite_ids(session, user))
        return ScanOut(found=True, barcode=barcode, food=_food_read(existing, user.id, favourites))

    product = await food_lookup.lookup(barcode)
    if product is None:
        return ScanOut(
            found=False,
            barcode=barcode,
            message="Not in Open Food Facts — add it once from the label and it stays.",
        )
    units = {}
    if product.serving_grams:
        units["piece"] = product.serving_grams
    food = Food(
        user_id=user.id,
        name=product.name,
        brand=product.brand,
        search_key=nutrition.key_of(product.name, [product.brand or ""]),
        kcal=product.kcal,
        protein=product.protein,
        carbs=product.carbs,
        fat=product.fat,
        base_unit=product.base_unit,
        units=units or None,
        barcode=barcode,
        source="barcode",
    )
    session.add(food)
    await session.flush()
    return ScanOut(found=True, barcode=barcode, food=_food_read(food, user.id, set()))


@router.post("/foods/scan", response_model=ScanOut)
async def scan_food(session: SessionDep, user: CurrentUser, photo: UploadFile = File()) -> ScanOut:
    """A photo of the barcode, for phones whose camera the page cannot use
    live: read the digits here, then look them up."""
    # One byte past the limit is enough to know — never the whole upload in memory
    content = await photo.read(MAX_PHOTO_BYTES + 1)
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="That photo is over 12 MB",
        )
    # Decoding a phone photo takes a while; off the event loop, it holds up
    # nobody else's request
    barcode = await asyncio.to_thread(food_lookup.read_barcode, content)
    if barcode is None:
        return ScanOut(found=False, message="No barcode in that photo — try filling the frame.")
    return await _food_for_barcode(session, user, barcode)


@router.get("/foods/barcode/{barcode}", response_model=ScanOut)
async def food_by_barcode(
    session: SessionDep,
    user: CurrentUser,
    barcode: str = Path(pattern=r"^\d{6,14}$"),
) -> ScanOut:
    """The digits the live camera scanner read (or someone typed off the
    packet): EAN-13, EAN-8, UPC-A/E or GTIN-14."""
    return await _food_for_barcode(session, user, barcode)


# --- Reading what was typed ---------------------------------------------------


@router.post("/parse", response_model=ParseOut)
async def parse_text(payload: ParseIn, session: SessionDep, user: CurrentUser) -> ParseOut:
    """ "50g ovsenih, 1 merica whey, 1 banana" — into amounts we can add up.
    A sentence said out loud often names its meal too ("… za ručak", "for
    breakfast …"): that is read off as the slot and left out of the food."""
    foods = await _foods_for(session, user)
    by_id = {food.id: food for food in foods}
    slot, text = slots.take_slot(payload.text)
    found, unknown = nutrition.parse(text, [_as_food_like(food) for food in foods])
    items = []
    for row in found:
        food = by_id.get(row.food.id) if row.food else None
        items.append(
            ParsedItem(
                food_id=food.id if food else None,
                label=row.label,
                quantity=row.quantity,
                unit=row.unit,
                grams=row.grams,
                **nutrition.macros(_as_food_like(food) if food else None, row.grams),
            )
        )
    return ParseOut(items=items, unknown=unknown, slot=slot)


# --- The diary ----------------------------------------------------------------


async def _meals_between(session, user, start: date, end: date) -> list[Meal]:
    rows = (
        await session.execute(
            select(Meal)
            .where(
                Meal.user_id == user.id,
                Meal.day >= start,
                Meal.day <= end,
            )
            .order_by(Meal.day, Meal.at.nulls_last(), Meal.created_at)
        )
    ).scalars()
    return list(rows)


@router.get("/days", response_model=list[DayTotals])
async def list_days(
    session: SessionDep,
    user: CurrentUser,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
) -> list[DayTotals]:
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from is after to")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="range is too long")
    days: dict[date, list] = {}
    counts: dict[date, int] = {}
    for meal in await _meals_between(session, user, start, end):
        days.setdefault(meal.day, []).extend(item.macros for item in meal.items)
        counts[meal.day] = counts.get(meal.day, 0) + 1
    return [
        DayTotals(day=day, meals=counts.get(day, 0), **nutrition.total(macros))
        for day, macros in sorted(days.items())
    ]


@router.get("/days/{day}", response_model=DayRead)
async def read_day(day: date, session: SessionDep, user: CurrentUser) -> DayRead:
    meals = [_meal_read(meal) for meal in await _meals_between(session, user, day, day)]
    totals = nutrition.total([meal.model_dump() for meal in meals])
    water = await body.water_totals(session, user, day, day)
    weight = await body.weights_between(session, user, day, day)
    return DayRead(
        day=day,
        totals=Macros(**totals),
        target=_target(user),
        meals=meals,
        water_ml=water.get(day, 0),
        water_goal_ml=body.water_goal(user),
        weight_kg=weight[0].kg if weight else None,
    )


async def _own_meal(session, user, meal_id: uuid.UUID) -> Meal:
    meal = await session.get(Meal, meal_id)
    if meal is None or meal.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such meal")
    return meal


def _diary_amount(quantity: float, unit: str | None) -> tuple[float, str]:
    """An amount as the diary keeps it. A kilo or a litre — what the parser
    reads out of "1 kg piletine" — becomes grams or millilitres with its
    quantity scaled, rather than falling back to a gram and keeping the 1."""
    quantity, unit = nutrition.in_base_units(quantity, unit or "g")
    return quantity, unit if unit in UNITS else "g"


def _build_item(write: ItemWrite, food: Food | None, position: int) -> MealItem:
    if food is None and write.macros is not None:
        # Numbers with no food behind them: 100 g "of the plate" carries them,
        # the same trick a recipe known only by its stated numbers uses, so a
        # second serving later is simply quantity 2
        return MealItem(
            position=position,
            food_id=None,
            label=(write.label or "Quick add")[:160],
            quantity=write.quantity,
            unit="serving",
            grams=round(100 * write.quantity, 2),
            kcal100=write.macros.kcal,
            protein100=write.macros.protein,
            carbs100=write.macros.carbs,
            fat100=write.macros.fat,
        )
    quantity, unit = _diary_amount(write.quantity, write.unit)
    like = _as_food_like(food) if food else None
    grams = nutrition.grams_for(like, quantity, unit)
    return MealItem(
        position=position,
        food_id=food.id if food else None,
        label=(write.label or (food.name if food else "Item"))[:160],
        quantity=quantity,
        unit=unit,
        grams=grams,
        kcal100=food.kcal if food else 0,
        protein100=food.protein if food else 0,
        carbs100=food.carbs if food else 0,
        fat100=food.fat if food else 0,
    )


async def _foods_by_id(session, user, ids: set[uuid.UUID]) -> dict[uuid.UUID, Food]:
    wanted = {value for value in ids if value}
    if not wanted:
        return {}
    query = select(Food).where(Food.id.in_(wanted), _visible_to(user))
    return {food.id: food for food in (await session.execute(query)).scalars()}


@router.post("/meals", response_model=MealRead, status_code=status.HTTP_201_CREATED)
async def create_meal(payload: MealWrite, session: SessionDep, user: CurrentUser) -> MealRead:
    foods = await _foods_by_id(session, user, {item.food_id for item in payload.items})
    items = [
        _build_item(item, foods.get(item.food_id) if item.food_id else None, index)
        for index, item in enumerate(payload.items)
    ]
    slot = slots.slot_of(payload.slot, payload.at, payload.title)
    # One food is best called by its name; a plate of several by its slot
    title = (payload.title or "").strip() or (
        items[0].label if len(items) == 1 else slots.LABELS[slot]
    )
    meal = Meal(
        user_id=user.id,
        day=payload.day,
        at=payload.at,
        title=title[:160],
        slot=slot,
        note=payload.note,
        items=items,
    )
    session.add(meal)
    await session.flush()
    return _meal_read(meal)


@router.post("/meals/from-recipe", response_model=MealRead, status_code=status.HTTP_201_CREATED)
async def meal_from_recipe(payload: FromRecipe, session: SessionDep, user: CurrentUser) -> MealRead:
    """Cook a recipe into the diary. Its amounts are copied and scaled —
    the copy is yours to correct afterwards, the recipe stays as it is."""
    recipe = await session.get(Recipe, payload.recipe_id)
    if recipe is None or recipe.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such recipe")
    share = payload.servings / (recipe.servings or 1)
    foods = await _foods_by_id(session, user, {item.food_id for item in recipe.items})
    meal = Meal(
        user_id=user.id,
        day=payload.day,
        at=payload.at,
        title=recipe.title,
        slot=slots.slot_of(payload.slot, payload.at, recipe.title),
        recipe_id=recipe.id,
        recipe_title=recipe.title,
        servings=payload.servings,
        items=[],
    )
    for index, item in enumerate(recipe.items):
        food = foods.get(item.food_id) if item.food_id else None
        meal.items.append(
            MealItem(
                position=index,
                food_id=item.food_id,
                label=item.label,
                quantity=round(item.quantity * share, 2),
                unit=item.unit,
                grams=round(item.grams * share, 1),
                kcal100=food.kcal if food else 0,
                protein100=food.protein if food else 0,
                carbs100=food.carbs if food else 0,
                fat100=food.fat if food else 0,
            )
        )

    # A dish that arrived as a line of numbers has nothing to copy, but it is
    # still a plate that was eaten. The stated numbers are the whole dish, so
    # one item carries ONE serving of them per 100 g and weighs 100 g a
    # serving — the same shape as a quick-kcal line, so changing how many
    # servings were eaten later is simply a new quantity.
    if not meal.items and isinstance(recipe.stated, dict):
        stated = recipe.stated
        per_serving = 1 / (recipe.servings or 1)
        meal.items.append(
            MealItem(
                position=0,
                food_id=None,
                label=recipe.title,
                quantity=payload.servings,
                unit="serving",
                grams=round(100 * payload.servings, 2),
                kcal100=float(stated.get("kcal") or 0) * per_serving,
                protein100=float(stated.get("protein") or 0) * per_serving,
                carbs100=float(stated.get("carbs") or 0) * per_serving,
                fat100=float(stated.get("fat") or 0) * per_serving,
            )
        )

    session.add(meal)
    await session.flush()
    return _meal_read(meal)


REQUIRED_MEAL_FIELDS = ("day", "slot", "title")


@router.patch("/meals/{meal_id}", response_model=MealRead)
async def update_meal(
    meal_id: uuid.UUID, payload: MealPatch, session: SessionDep, user: CurrentUser
) -> MealRead:
    meal = await _own_meal(session, user, meal_id)
    fields = payload.model_dump(exclude_unset=True)
    for name, value in fields.items():
        # A meal always has a day, a slot and a name: a null for one of them
        # leaves it as it was, the way a null title always has
        if name in REQUIRED_MEAL_FIELDS and not value:
            continue
        setattr(meal, name, value)
    await session.flush()
    return _meal_read(meal)


def _copy_of(meal: Meal, day: date, at, slot: str | None) -> Meal:
    """A new meal with the same plate: the items keep the numbers they were
    eaten at, so a copy of last Tuesday's lunch adds up exactly as it did.
    The recording stays with the original — it was said once."""
    return Meal(
        user_id=meal.user_id,
        day=day,
        at=meal.at if at is None else at,
        title=meal.title,
        slot=slot or meal.slot,
        recipe_id=meal.recipe_id,
        recipe_title=meal.recipe_title,
        servings=meal.servings,
        note=meal.note,
        items=[
            MealItem(
                position=item.position,
                food_id=item.food_id,
                label=item.label,
                quantity=item.quantity,
                unit=item.unit,
                grams=item.grams,
                kcal100=item.kcal100,
                protein100=item.protein100,
                carbs100=item.carbs100,
                fat100=item.fat100,
            )
            for item in meal.items
        ],
    )


@router.post("/meals/{meal_id}/copy", response_model=MealRead, status_code=status.HTTP_201_CREATED)
async def copy_meal(
    meal_id: uuid.UUID, payload: MealCopy, session: SessionDep, user: CurrentUser
) -> MealRead:
    meal = await _own_meal(session, user, meal_id)
    copy = _copy_of(meal, payload.day, payload.at, payload.slot)
    session.add(copy)
    await session.flush()
    return _meal_read(copy)


@router.post("/days/{day}/copy", response_model=list[MealRead], status_code=status.HTTP_201_CREATED)
async def copy_day(
    day: date, payload: DayCopy, session: SessionDep, user: CurrentUser
) -> list[MealRead]:
    """Another day's meals onto this one — all of them, or one slot's
    ("yesterday's breakfast again")."""
    source = await _meals_between(session, user, payload.from_day, payload.from_day)
    if payload.slot:
        source = [meal for meal in source if meal.slot == payload.slot]
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Nothing to copy on that day"
        )
    copies = [_copy_of(meal, day, None, None) for meal in source]
    session.add_all(copies)
    await session.flush()
    return [_meal_read(meal) for meal in copies]


MEAL_COLUMNS = ("day", "at", "title", "slot", "recipe_id", "recipe_title", "servings", "note")
MEAL_VOICE_COLUMNS = ("voice_type", "voice_seconds", "voice_transcribed")
ITEM_COLUMNS = (
    "id",
    "position",
    "food_id",
    "label",
    "quantity",
    "unit",
    "grams",
    "kcal100",
    "protein100",
    "carbs100",
    "fat100",
)


def _plain(value):
    """JSON keeps strings, so ids, dates and times travel as their ISO text."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date | time):
        return value.isoformat()
    return value


@router.delete("/meals/{meal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meal(meal_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    """Gone from the diary at once — the meal and its items are deleted —
    but a copy is kept for a day so the toast's Undo can bring it back."""
    meal = await _own_meal(session, user, meal_id)
    await session.refresh(meal, attribute_names=["voice"])
    snapshot = {name: _plain(getattr(meal, name)) for name in (*MEAL_COLUMNS, *MEAL_VOICE_COLUMNS)}
    snapshot["items"] = [
        {name: _plain(getattr(item, name)) for name in ITEM_COLUMNS} for item in meal.items
    ]
    existing = await session.get(DeletedMeal, meal.id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()
    session.add(DeletedMeal(id=meal.id, user_id=user.id, snapshot=snapshot, voice=meal.voice))
    await session.delete(meal)
    await session.flush()


@router.post("/meals/{meal_id}/restore", response_model=MealRead)
async def restore_meal(meal_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> MealRead:
    """The Undo of a delete: the very same meal comes back, items, numbers
    and recording as they were."""
    kept = await session.get(DeletedMeal, meal_id)
    if kept is None or kept.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nothing to restore")
    await session.refresh(kept, attribute_names=["voice"])
    snapshot = kept.snapshot
    recipe_id = uuid.UUID(snapshot["recipe_id"]) if snapshot.get("recipe_id") else None
    # The recipe may have been deleted in between; the meal keeps its title
    if recipe_id and await session.get(Recipe, recipe_id) is None:
        recipe_id = None
    rows = snapshot.get("items", [])
    # A food deleted for good since then leaves its line, without the link
    known = await _foods_by_id(
        session, user, {uuid.UUID(row["food_id"]) for row in rows if row.get("food_id")}
    )

    def item_of(row: dict) -> MealItem:
        food_id = uuid.UUID(row["food_id"]) if row.get("food_id") else None
        return MealItem(
            **{**row, "id": uuid.UUID(row["id"]), "food_id": food_id if food_id in known else None}
        )

    meal = Meal(
        id=kept.id,
        user_id=user.id,
        day=date.fromisoformat(snapshot["day"]),
        at=time.fromisoformat(snapshot["at"]) if snapshot.get("at") else None,
        title=snapshot["title"],
        slot=snapshot.get("slot") or "snack",
        recipe_id=recipe_id,
        recipe_title=snapshot.get("recipe_title"),
        servings=snapshot.get("servings") or 1,
        note=snapshot.get("note"),
        voice=kept.voice,
        voice_type=snapshot.get("voice_type"),
        voice_seconds=snapshot.get("voice_seconds"),
        voice_transcribed=bool(snapshot.get("voice_transcribed")),
        items=[item_of(row) for row in rows],
    )
    await session.delete(kept)
    await session.flush()
    session.add(meal)
    await session.flush()
    return _meal_read(meal)


# Two minutes of speech is a long description of a plate; anything past that is
# a pocket recording nobody meant to keep.
MAX_VOICE_BYTES = 8 * 1024 * 1024
VOICE_TYPES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac")


@router.post("/meals/{meal_id}/voice", response_model=MealRead)
async def attach_voice(
    meal_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    seconds: Annotated[float | None, Form(ge=0, le=3600)] = None,
    transcribed: Annotated[bool, Form()] = False,
) -> MealRead:
    """Hang the recording on the meal.

    The browser transcribes when it can and the words land in `note` like any
    other; the audio is kept regardless, because dictation in the language the
    plate was described in is often missing from exactly the device this is
    for — a phone, in a kitchen.
    """
    meal = await _own_meal(session, user, meal_id)
    content = await file.read(MAX_VOICE_BYTES + 1)

    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty recording")
    if len(content) > MAX_VOICE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="That recording is too long — keep it under two minutes.",
        )

    # A browser sends "audio/webm;codecs=opus"; the type is what plays it back
    kind = (file.content_type or "").split(";")[0].strip().lower()
    if kind not in VOICE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Not an audio recording ({file.content_type or 'unknown'})",
        )

    meal.voice = content
    meal.voice_type = kind
    meal.voice_seconds = round(seconds, 1) if seconds else None
    meal.voice_transcribed = transcribed
    await session.flush()
    return _meal_read(meal)


@router.get("/meals/{meal_id}/voice")
async def play_voice(meal_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Response:
    """The recording itself. The column is deferred, so this is the one place
    the bytes are ever read."""
    meal = await _own_meal(session, user, meal_id)
    # Deferred columns are not loaded with the row; ask for this one by name
    await session.refresh(meal, attribute_names=["voice"])
    content = meal.voice

    if not content or not meal.voice_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No recording")

    return Response(
        content=content,
        media_type=meal.voice_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/meals/{meal_id}/voice", response_model=MealRead)
async def drop_voice(meal_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> MealRead:
    """The words stay; only the recording goes."""
    meal = await _own_meal(session, user, meal_id)
    meal.voice = None
    meal.voice_type = None
    meal.voice_seconds = None
    meal.voice_transcribed = False
    await session.flush()
    return _meal_read(meal)


@router.post("/meals/{meal_id}/items", response_model=MealRead)
async def add_item(
    meal_id: uuid.UUID, payload: ItemWrite, session: SessionDep, user: CurrentUser
) -> MealRead:
    meal = await _own_meal(session, user, meal_id)
    foods = await _foods_by_id(session, user, {payload.food_id})
    # An Undo puts a removed line back where it was; anything else goes last
    position = len(meal.items)
    if payload.position is not None and payload.position < len(meal.items):
        position = payload.position
        for item in meal.items:
            if item.position >= position:
                item.position += 1
    meal.items.append(
        _build_item(payload, foods.get(payload.food_id) if payload.food_id else None, position)
    )
    meal.items.sort(key=lambda item: item.position)
    await session.flush()
    return _meal_read(meal)


@router.patch("/meals/{meal_id}/items/{item_id}", response_model=MealRead)
async def update_item(
    meal_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: ItemPatch,
    session: SessionDep,
    user: CurrentUser,
) -> MealRead:
    meal = await _own_meal(session, user, meal_id)
    item = next((row for row in meal.items if row.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such item")
    fields = payload.model_dump(exclude_unset=True)
    if "food_id" in fields and fields["food_id"]:
        foods = await _foods_by_id(session, user, {fields["food_id"]})
        food = foods.get(fields["food_id"])
        if food is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such food")
        item.food_id = food.id
        item.label = food.name
        item.kcal100, item.protein100 = food.kcal, food.protein
        item.carbs100, item.fat100 = food.carbs, food.fat
    if fields.get("label"):
        item.label = fields["label"][:160]
    if "quantity" in fields and fields["quantity"] is not None:
        item.quantity = fields["quantity"]
    if fields.get("unit") in (*UNITS, *nutrition.SCALED_UNITS):
        item.quantity, item.unit = _diary_amount(item.quantity, fields["unit"])
    food = (await _foods_by_id(session, user, {item.food_id})).get(item.food_id)
    item.grams = nutrition.grams_for(
        _as_food_like(food) if food else None, item.quantity, item.unit
    )
    await session.flush()
    return _meal_read(meal)


@router.delete("/meals/{meal_id}/items/{item_id}", response_model=MealRead)
async def delete_item(
    meal_id: uuid.UUID, item_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> MealRead:
    meal = await _own_meal(session, user, meal_id)
    item = next((row for row in meal.items if row.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such item")
    meal.items.remove(item)
    await session.flush()
    return _meal_read(meal)


# --- Recipes ------------------------------------------------------------------


@router.get("/recipes", response_model=list[RecipeRead])
async def list_recipes(
    session: SessionDep,
    user: CurrentUser,
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[RecipeRead]:
    recipes = list(
        (
            await session.execute(
                select(Recipe).where(Recipe.user_id == user.id).order_by(Recipe.title)
            )
        ).scalars()
    )
    if q:
        wanted = nutrition.normalize(q)
        recipes = [
            recipe
            for recipe in recipes
            if wanted in nutrition.normalize(f"{recipe.title} {recipe.subtitle or ''}")
        ]
    recipes = recipes[:limit]
    foods = await _foods_by_id(
        session, user, {item.food_id for recipe in recipes for item in recipe.items}
    )
    return [_recipe_read(recipe, foods) for recipe in recipes]


RECIPE_FIELDS = (
    "title",
    "stated",
    "subtitle",
    "servings",
    "serving_unit",
    "minutes",
    "steps",
    "note",
)


async def _own_recipe(session, user, recipe_id: uuid.UUID) -> Recipe:
    recipe = await session.get(Recipe, recipe_id)
    if recipe is None or recipe.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such recipe")
    return recipe


@router.get("/recipes/{recipe_id}", response_model=RecipeRead)
async def read_recipe(recipe_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> RecipeRead:
    recipe = await _own_recipe(session, user, recipe_id)
    foods = await _foods_by_id(session, user, {item.food_id for item in recipe.items})
    return _recipe_read(recipe, foods)


async def _apply_items(session, user, recipe: Recipe, items: list) -> None:
    foods = await _foods_by_id(session, user, {item.food_id for item in items})
    recipe.items = []
    for index, write in enumerate(items):
        food = foods.get(write.food_id) if write.food_id else None
        quantity, unit = _diary_amount(write.quantity, write.unit)
        recipe.items.append(
            RecipeItem(
                position=index,
                food_id=food.id if food else None,
                label=(write.label or (food.name if food else "Ingredient"))[:160],
                quantity=quantity,
                unit=unit,
                grams=nutrition.grams_for(_as_food_like(food) if food else None, quantity, unit),
                optional=write.optional,
            )
        )


@router.post("/recipes", response_model=RecipeRead, status_code=status.HTTP_201_CREATED)
async def create_recipe(payload: RecipeWrite, session: SessionDep, user: CurrentUser) -> RecipeRead:
    recipe = Recipe(
        user_id=user.id,
        title=payload.title.strip(),
        subtitle=payload.subtitle,
        servings=payload.servings,
        # The schema is a Literal, so there is nothing left to sanitise here
        serving_unit=payload.serving_unit,
        stated=payload.stated.model_dump() if payload.stated else None,
        minutes=payload.minutes,
        steps=payload.steps,
        note=payload.note,
    )
    # The ingredients go on before the recipe joins the session. Inside
    # `_apply_items` the food lookup is a SELECT, which autoflushes whatever is
    # pending — and a flushed recipe then tries to LOAD its items collection
    # the moment we assign to it, which is IO from a sync context and blows up.
    # Transient until it is furnished: nothing to flush, nothing to load.
    await _apply_items(session, user, recipe, payload.items)
    session.add(recipe)
    await session.flush()
    foods = await _foods_by_id(session, user, {item.food_id for item in recipe.items})
    return _recipe_read(recipe, foods)


@router.patch("/recipes/{recipe_id}", response_model=RecipeRead)
async def update_recipe(
    recipe_id: uuid.UUID, payload: RecipePatch, session: SessionDep, user: CurrentUser
) -> RecipeRead:
    recipe = await _own_recipe(session, user, recipe_id)
    fields = payload.model_dump(exclude_unset=True)
    for name in RECIPE_FIELDS:
        # Only the optional words and the minutes can be cleared; a title,
        # a serving count or the steps sent as null leave things as they were
        if name in fields and (fields[name] is not None or name in ("subtitle", "note", "minutes")):
            setattr(recipe, name, fields[name])
    if "items" in fields and payload.items is not None:
        await _apply_items(session, user, recipe, payload.items)
    await session.flush()
    foods = await _foods_by_id(session, user, {item.food_id for item in recipe.items})
    return _recipe_read(recipe, foods)


@router.delete("/recipes/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recipe(recipe_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    """The meals cooked from it keep their items and its title."""
    recipe = await _own_recipe(session, user, recipe_id)
    # The foreign key would null these too, but saying it here also keeps the
    # meals already loaded in this session honest
    await session.execute(
        update(Meal)
        .where(Meal.recipe_id == recipe.id, Meal.user_id == user.id)
        .values(recipe_id=None)
    )
    await session.delete(recipe)
    await session.flush()


# --- The printed diary --------------------------------------------------------


@router.get("/export")
async def export_days(
    session: SessionDep,
    user: CurrentUser,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
    fmt: str = Query(default="pdf", alias="format", pattern="^(pdf|csv)$"),
) -> Response:
    """The diary for a period, day by day — as a printable PDF or a CSV."""
    from app.services import eating_report

    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from is after to")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="range is too long")
    meals = [_meal_read(meal) for meal in await _meals_between(session, user, start, end)]
    days = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        on_day = [meal for meal in meals if meal.day == day]
        if on_day:
            days.append((day, on_day, nutrition.total([meal.model_dump() for meal in on_day])))
    name = f"Nutrijurnal_{start:%Y-%m-%d}_{end:%Y-%m-%d}"
    if fmt == "csv":
        body = eating_report.build_csv(days)
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    body = eating_report.build_pdf(
        days,
        who=(user.full_name or user.email.split("@")[0]),
        target=_target(user),
        generated_at=datetime.now(UTC),
    )
    return Response(
        content=body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
    )
