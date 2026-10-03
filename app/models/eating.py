"""
The diary: what was eaten, when, and what it added up to.

Five tables and two rules. `foods` holds nutrition per 100 g — the seeded
staples are shared by everyone and belong to nobody, while anything a person
types in or scans belongs to that person alone. `recipes` are each person's
own dishes, built from foods. `meals` are the diary itself, and `meal_items`
carry a COPY of the food's per-100 g numbers: correcting a food tomorrow must
never silently rewrite what yesterday's breakfast came to.

The second rule is the public one: every row a person writes carries their
`user_id`, and every query on those rows filters on it.
"""

import uuid
from datetime import date, time

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

# What an amount can be counted in. Grams and millilitres are the truth;
# the rest are conveniences resolved to grams through the food's own table
# ("1 scoop of whey" = 30 g) or a sensible default.
UNITS = ("g", "ml", "piece", "scoop", "tbsp", "tsp", "cup", "handful", "pinch", "slice")


class Food(UUIDMixin, TimestampMixin, Base):
    """A thing you can eat, with its numbers per 100 g (or 100 ml)."""

    __tablename__ = "foods"

    # None = a seeded food everyone shares and nobody may edit; set = one
    # person's own addition, invisible to everyone else
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Stable slug for the seed, so re-seeding updates instead of duplicating.
    # Unique, so two workers seeding at once can't both insert the same food.
    key: Mapped[str | None] = mapped_column(String(80), unique=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(120))
    # Name and aliases, lowercased and stripped of diacritics — what the
    # free-text parser and the search box actually match against
    search_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aliases: Mapped[list | None] = mapped_column(JSON)

    kcal: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    protein: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    carbs: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    fat: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    base_unit: Mapped[str] = mapped_column(String(4), nullable=False, default="g")
    # grams per named unit: {"piece": 55, "scoop": 30, "tbsp": 16}
    units: Mapped[dict | None] = mapped_column(JSON)

    barcode: Mapped[str | None] = mapped_column(String(32), index=True)
    # seed · manual · barcode (Open Food Facts)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<Food {self.name!r} {self.kcal}kcal/100{self.base_unit}>"


class Recipe(UUIDMixin, TimestampMixin, Base):
    """A dish someone makes: the ingredients, the steps, the servings."""

    __tablename__ = "recipes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(160))

    servings: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    # serving · piece (a batch of twelve muffins is counted in pieces)
    serving_unit: Mapped[str] = mapped_column(String(8), nullable=False, default="serving")
    minutes: Mapped[int | None] = mapped_column(Integer)

    steps: Mapped[list | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    # Numbers copied off a label or a meal plan: {"kcal": 742, "protein": 24, ...}
    # for the whole dish. A dish can be known by these alone, before anyone
    # writes its ingredients down — and it can still be eaten.
    stated: Mapped[dict | None] = mapped_column(JSON)

    items: Mapped[list["RecipeItem"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="RecipeItem.position",
    )

    def __repr__(self) -> str:
        return f"<Recipe {self.title!r}>"


class RecipeItem(UUIDMixin, Base):
    """One ingredient line, as an amount of a food."""

    __tablename__ = "recipe_items"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    food_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"))

    label: Mapped[str] = mapped_column(String(160), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, default="g")
    grams: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    # Salt, spices, "a bit of salad" — counted, but nobody weighs them
    optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    recipe: Mapped[Recipe] = relationship(back_populates="items")


class Meal(UUIDMixin, TimestampMixin, Base):
    """One entry in the diary: a time, a name, and what went into it."""

    __tablename__ = "meals"
    # Every read of the diary is "this person, these days" — one index answers it
    __table_args__ = (Index("ix_meals_user_id_day", "user_id", "day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    at: Mapped[time | None] = mapped_column(Time)
    title: Mapped[str] = mapped_column(String(160), nullable=False)

    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL")
    )
    # Kept as text too: deleting a recipe must not blank the diary's history
    recipe_title: Mapped[str | None] = mapped_column(String(160))
    servings: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)

    # --- Said out loud, with both hands busy ----------------------------------
    # A meal can arrive as a voice note: the browser transcribes it when it can,
    # and the recording is kept either way so nothing is lost to a phone that
    # cannot dictate the language it was said in.
    # Deferred: a week of the diary must never drag its audio into memory —
    # only the endpoint that plays one loads it.
    voice: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    voice_type: Mapped[str | None] = mapped_column(String(60))
    voice_seconds: Mapped[float | None] = mapped_column(Float)
    # Whether the words in `note` came from the browser's own dictation, so a
    # transcript nobody has checked is never mistaken for something typed
    voice_transcribed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    items: Mapped[list["MealItem"]] = relationship(
        back_populates="meal",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="MealItem.position",
    )

    def __repr__(self) -> str:
        return f"<Meal {self.day} {self.title!r}>"


class MealItem(UUIDMixin, Base):
    """An amount of a food inside a meal, with the food's numbers copied in."""

    __tablename__ = "meal_items"

    meal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    food_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"))

    label: Mapped[str] = mapped_column(String(160), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, default="g")
    grams: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # The food's per-100 g numbers as they were when this was eaten
    kcal100: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    protein100: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    carbs100: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    fat100: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    meal: Mapped[Meal] = relationship(back_populates="items")

    @property
    def macros(self) -> dict[str, float]:
        share = (self.grams or 0) / 100
        return {
            "kcal": round(self.kcal100 * share, 1),
            "protein": round(self.protein100 * share, 1),
            "carbs": round(self.carbs100 * share, 1),
            "fat": round(self.fat100 * share, 1),
        }
