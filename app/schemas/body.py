"""Water and weight, as the diary sends and accepts them."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class WaterWrite(BaseModel):
    day: date
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
