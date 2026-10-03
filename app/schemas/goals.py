"""The questions behind a daily target, and what they come to."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Sex = Literal["female", "male", "other"]
Activity = Literal["sedentary", "light", "moderate", "active", "very_active"]
Goal = Literal["lose", "maintain", "gain"]

# Younger than this and a calorie target is a conversation for a doctor, not an app
MIN_AGE = 13

# The bodies the formulas were fitted on. A scale may read outside this (a
# weighing takes 20–400 kg), but the calculator never starts from such a number.
MIN_WEIGHT_KG = 30
MAX_WEIGHT_KG = 350


class GoalProfile(BaseModel):
    sex: Sex
    birth_year: int = Field(ge=1900)
    height_cm: float = Field(ge=100, le=250)
    weight_kg: float = Field(ge=MIN_WEIGHT_KG, le=MAX_WEIGHT_KG)
    activity: Activity = "light"
    goal: Goal = "maintain"
    # Kilograms a week. Ignored when maintaining; capped per goal by the
    # calculator, so a too-eager number is softened rather than refused.
    pace: float = Field(default=0.5, ge=0, le=1)
    # None takes the goal's own default (more protein while losing)
    protein_per_kg: float | None = Field(default=None, ge=0.8, le=3)
    fat_percent: int = Field(default=30, ge=15, le=45)

    @field_validator("birth_year")
    @classmethod
    def _old_enough(cls, value: int) -> int:
        if value > date.today().year - MIN_AGE:
            raise ValueError(f"Nutrijurnal's targets are for people aged {MIN_AGE} and over")
        return value


class GoalEstimate(BaseModel):
    age: int
    # Burned at rest (Mifflin–St Jeor) and on an ordinary day (× activity)
    bmr: int
    maintenance: int
    # What the day should come to, and the macros it is split into
    kcal: int
    protein: int
    carbs: int
    fat: int
    # The adjustment applied for the goal: negative is a deficit
    daily_change: int
    protein_per_kg: float
    fat_percent: int
    # True when the goal asked for less than the safe minimum and was raised
    floored: bool = False
