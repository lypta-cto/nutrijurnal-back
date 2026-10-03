"""What the diary sends and accepts."""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.goals import GoalProfile

# Where a meal sits in the day — the diary groups by it (services/slots.py)
Slot = Literal["breakfast", "lunch", "dinner", "snack"]

# Bounds on what a person can write into a public database: generous for any
# real recipe, small enough that one request can't store a novel
Step = Annotated[str, Field(max_length=2000)]
Alias = Annotated[str, Field(max_length=120)]
NOTE_MAX = 4000

# The most a food's own portion may weigh — a family pizza, a big tub. Past
# it a slipped digit (or a 1e308) makes every total of the day infinite, and
# the diary, Progress and the export answer it as null
MAX_PORTION_GRAMS = 5000
Portion = Annotated[float, Field(le=MAX_PORTION_GRAMS)]
# Per 100 g, nothing has more energy than pure fat or more of a macro than all of it
MAX_KCAL_PER_100 = 1000
MAX_MACRO_PER_100 = 100


class Macros(BaseModel):
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0


class MacrosWrite(BaseModel):
    """Numbers typed straight in — bounded, unlike the ones the server adds up."""

    kcal: float = Field(default=0, ge=0, le=20000)
    protein: float = Field(default=0, ge=0, le=2000)
    carbs: float = Field(default=0, ge=0, le=2000)
    fat: float = Field(default=0, ge=0, le=2000)


class FoodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    name_en: str | None = None
    brand: str | None = None
    kcal: float
    protein: float
    carbs: float
    fat: float
    base_unit: str
    units: dict[str, float] = {}
    barcode: str | None = None
    source: str
    archived: bool = False
    # Yours to edit, or one of the shared staples nobody may change
    mine: bool = False
    # Starred, so it comes first when adding food
    favourite: bool = False

    @field_validator("units", mode="before")
    @classmethod
    def _no_units_is_an_empty_dict(cls, value: dict | None) -> dict:
        """A food with nothing but grams is stored with `units = NULL`, and
        reading it must not 500."""
        return value or {}


class FoodPick(FoodRead):
    """A food offered for a quick add, with the amount it was last eaten in —
    one tap writes it down again the same way."""

    last_quantity: float | None = None
    last_unit: str | None = None
    last_day: date | None = None


class QuickFoods(BaseModel):
    favourites: list[FoodPick] = []
    recent: list[FoodPick] = []


class FoodWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    name_en: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    kcal: float = Field(ge=0, le=MAX_KCAL_PER_100)
    protein: float = Field(default=0, ge=0, le=MAX_MACRO_PER_100)
    carbs: float = Field(default=0, ge=0, le=MAX_MACRO_PER_100)
    fat: float = Field(default=0, ge=0, le=MAX_MACRO_PER_100)
    base_unit: str = "g"
    # Grams per portion; a unit the diary doesn't know, or a portion of
    # nothing, is dropped rather than refused
    units: dict[str, Portion] = Field(default={}, max_length=20)
    barcode: str | None = Field(default=None, max_length=32)
    aliases: list[Alias] = Field(default=[], max_length=20)


class FoodPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    name_en: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    kcal: float | None = Field(default=None, ge=0, le=MAX_KCAL_PER_100)
    protein: float | None = Field(default=None, ge=0, le=MAX_MACRO_PER_100)
    carbs: float | None = Field(default=None, ge=0, le=MAX_MACRO_PER_100)
    fat: float | None = Field(default=None, ge=0, le=MAX_MACRO_PER_100)
    base_unit: str | None = None
    units: dict[str, Portion] | None = Field(default=None, max_length=20)
    archived: bool | None = None


class ItemWrite(BaseModel):
    """An amount of something, as the page sends it."""

    food_id: uuid.UUID | None = None
    label: str = Field(default="", max_length=160)
    quantity: float = Field(default=1, ge=0, le=10000)
    unit: str = "g"
    # A plate with no food behind it, known only by its numbers — "a slice of
    # cake at the office, ~350 kcal". Per one serving; ignored when food_id is set.
    macros: MacrosWrite | None = None
    # Where in the meal it goes — an Undo puts a removed line back in its place
    position: int | None = Field(default=None, ge=0, le=100)


class ItemPatch(BaseModel):
    food_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=160)
    quantity: float | None = Field(default=None, ge=0, le=10000)
    unit: str | None = None


class MealItemRead(Macros):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    food_id: uuid.UUID | None = None
    label: str
    quantity: float
    unit: str
    grams: float


class MealRead(Macros):
    id: uuid.UUID
    day: date
    at: time | None = None
    title: str
    slot: Slot = "snack"
    recipe_id: uuid.UUID | None = None
    recipe_title: str | None = None
    servings: float = 1
    note: str | None = None
    # A voice note lives beside the words: the recording is always kept, the
    # transcript only when the browser managed one
    has_voice: bool = False
    voice_seconds: float | None = None
    voice_transcribed: bool = False
    items: list[MealItemRead] = []


class MealWrite(BaseModel):
    day: date
    at: time | None = None
    # Left out: the one item's name, or the slot's ("Breakfast")
    title: str | None = Field(default=None, max_length=160)
    # Left out: taken from the time, then from the title, else a snack
    slot: Slot | None = None
    note: str | None = Field(default=None, max_length=4000)
    items: list[ItemWrite] = Field(default=[], max_length=100)


class MealPatch(BaseModel):
    day: date | None = None
    at: time | None = None
    title: str | None = Field(default=None, min_length=1, max_length=160)
    slot: Slot | None = None
    note: str | None = Field(default=None, max_length=4000)


class FromRecipe(BaseModel):
    recipe_id: uuid.UUID
    day: date
    at: time | None = None
    slot: Slot | None = None
    # How many servings of it were actually eaten
    servings: float = Field(default=1, gt=0, le=50)


class MealCopy(BaseModel):
    """The same plate again, onto another day — or the same one."""

    day: date
    # Left out: the time and the slot it had
    at: time | None = None
    slot: Slot | None = None


class DayCopy(BaseModel):
    """Everything eaten on one day (or only its breakfast) onto another."""

    from_day: date
    slot: Slot | None = None


class RecipeItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    food_id: uuid.UUID | None = None
    label: str
    quantity: float
    unit: str
    grams: float
    optional: bool = False


class RecipeRead(Macros):
    id: uuid.UUID
    title: str
    subtitle: str | None = None
    servings: float
    serving_unit: str
    minutes: int | None = None
    steps: list[str] = []
    note: str | None = None
    items: list[RecipeItemRead] = []
    # The numbers copied off a label or a plan, when there are any
    stated: Macros | None = None


class RecipeItemWrite(BaseModel):
    food_id: uuid.UUID | None = None
    label: str = Field(default="", max_length=160)
    quantity: float = Field(default=0, ge=0, le=10000)
    unit: str = "g"
    optional: bool = False


class RecipeWrite(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=160)
    servings: float = Field(default=1, gt=0, le=50)
    serving_unit: Literal["serving", "piece"] = "serving"
    # A dish can arrive as a line of numbers and nothing else —
    # "Kcal: 742, P: 24g, C: 98g, F: 25g" — and that is enough to eat it;
    # the ingredients can follow later, or never.
    stated: MacrosWrite | None = None
    minutes: int | None = Field(default=None, ge=0, le=600)
    steps: list[Step] = Field(default=[], max_length=100)
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    items: list[RecipeItemWrite] = Field(default=[], max_length=100)


class RecipePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=160)
    servings: float | None = Field(default=None, gt=0, le=50)
    serving_unit: Literal["serving", "piece"] | None = None
    stated: MacrosWrite | None = None
    minutes: int | None = Field(default=None, ge=0, le=600)
    steps: list[Step] | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    # Sent whole: this is how "five eggs, make it two" is saved
    items: list[RecipeItemWrite] | None = Field(default=None, max_length=100)


class DayTotals(Macros):
    day: date
    meals: int = 0


class DayRead(BaseModel):
    day: date
    totals: Macros
    target: Macros | None = None
    meals: list[MealRead] = []
    # Read beside the food, so Today needs one request
    water_ml: int = 0
    water_goal_ml: int = 2000
    weight_kg: float | None = None


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ParsedItem(Macros):
    food_id: uuid.UUID | None = None
    label: str
    quantity: float
    unit: str
    grams: float


class ParseOut(BaseModel):
    items: list[ParsedItem] = []
    unknown: list[str] = []
    # The slot the sentence named — "za ručak", "for breakfast" — if it did
    slot: Slot | None = None


class ScanOut(BaseModel):
    found: bool = False
    barcode: str | None = None
    food: FoodRead | None = None
    message: str | None = None


class SettingsRead(BaseModel):
    target_kcal: int | None = None
    target_protein: int | None = None
    target_carbs: int | None = None
    target_fat: int | None = None
    onboarded_at: datetime | None = None
    # The calculator's answers, when they were given
    profile: GoalProfile | None = None
    water_goal_ml: int = 2000
    water_glass_ml: int = 250
    # IANA timezone the reminders run in; null until the browser says
    timezone: str | None = None
    # How full the pantry and the recipe book are — the shared foods count too
    foods: int = 0
    recipes: int = 0


class SettingsPatch(BaseModel):
    target_kcal: int | None = Field(default=None, ge=0, le=20000)
    target_protein: int | None = Field(default=None, ge=0, le=1000)
    target_carbs: int | None = Field(default=None, ge=0, le=2000)
    target_fat: int | None = Field(default=None, ge=0, le=1000)
    water_goal_ml: int | None = Field(default=None, ge=250, le=10000)
    water_glass_ml: int | None = Field(default=None, ge=50, le=2000)
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _a_real_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown timezone") from error
        return value

    # True marks the first-run questions as answered (or skipped); it is never
    # unset, so sending false is simply ignored
    onboarded: bool | None = None
    # The answers the targets were worked out from, kept whole
    profile: GoalProfile | None = None
