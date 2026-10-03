"""Water and weight, as the diary sends and accepts them."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.eating import DiaryDay


class WaterWrite(BaseModel):
    day: DiaryDay
    # A glass, a bottle, a jug — never a bathtub
    ml: int = Field(gt=0, le=5000)


class WaterEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ml: int
    created_at: datetime


class WaterDay(BaseModel):
    day: date
    ml: int = 0
    goal_ml: int
    glass_ml: int
    entries: list[WaterEntryRead] = []


class WaterTotal(BaseModel):
    day: date
    ml: int


class WeightWrite(BaseModel):
    kg: float = Field(ge=20, le=400)


class WeightRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    kg: float


class ProgressDay(BaseModel):
    day: date
    meals: int = 0
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    water_ml: int = 0
    weight_kg: float | None = None


class Streak(BaseModel):
    # Days in a row with something written down, up to today (or yesterday,
    # while today is still empty)
    current: int = 0
    longest: int = 0
    logged_today: bool = False


class Averages(BaseModel):
    # Over the days that have meals — an unlogged day is unknown, not zero
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    # Over the days some water was logged
    water_ml: float | None = None
    logged_days: int = 0
    days: int = 0


class WeightChange(BaseModel):
    first: WeightRead | None = None
    last: WeightRead | None = None
    change: float | None = None


class ProgressRead(BaseModel):
    days: list[ProgressDay]
    target_kcal: int | None = None
    target_protein: int | None = None
    target_carbs: int | None = None
    target_fat: int | None = None
    water_goal_ml: int
    streak: Streak
    averages: Averages
    weight: WeightChange
