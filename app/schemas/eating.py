"""What the diary sends and accepts."""

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.eating import UNITS
from app.schemas.goals import GoalProfile

# Where a meal sits in the day — the diary groups by it (services/slots.py)
Slot = Literal["breakfast", "lunch", "dinner", "snack"]


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
    kcal: float = Field(ge=0, le=1000)
    protein: float = Field(default=0, ge=0, le=100)
    carbs: float = Field(default=0, ge=0, le=100)
    fat: float = Field(default=0, ge=0, le=100)
    base_unit: str = "g"
    units: dict[str, float] = {}
    barcode: str | None = Field(default=None, max_length=32)
    aliases: list[str] = []


class FoodPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    name_en: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    kcal: float | None = Field(default=None, ge=0, le=1000)
    protein: float | None = Field(default=None, ge=0, le=100)
    carbs: float | None = Field(default=None, ge=0, le=100)
    fat: float | None = Field(default=None, ge=0, le=100)
    base_unit: str | None = None
    units: dict[str, float] | None = None
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

    def unit_or_default(self) -> str:
        return self.unit if self.unit in UNITS else "g"


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
    steps: list[str] = []
    note: str | None = None
    items: list[RecipeItemWrite] = Field(default=[], max_length=100)


class RecipePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=160)
    servings: float | None = Field(default=None, gt=0, le=50)
    serving_unit: Literal["serving", "piece"] | None = None
    stated: MacrosWrite | None = None
    minutes: int | None = Field(default=None, ge=0, le=600)
    steps: list[str] | None = None
    note: str | None = None
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
    # How full the pantry and the recipe book are — the shared foods count too
    foods: int = 0
    recipes: int = 0


class SettingsPatch(BaseModel):
    target_kcal: int | None = Field(default=None, ge=0, le=20000)
    target_protein: int | None = Field(default=None, ge=0, le=1000)
    target_carbs: int | None = Field(default=None, ge=0, le=2000)
    target_fat: int | None = Field(default=None, ge=0, le=1000)
    # True marks the first-run questions as answered (or skipped); it is never
    # unset, so sending false is simply ignored
    onboarded: bool | None = None
    # The answers the targets were worked out from, kept whole
    profile: GoalProfile | None = None
