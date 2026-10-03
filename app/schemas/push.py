"""Push subscriptions and reminders, as the page sends and reads them."""

import uuid
from datetime import date, time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.eating import Slot

ReminderKind = Literal["meal", "water", "summary"]


class PushConfig(BaseModel):
    # False when the server has no VAPID keys: reminders are kept, not sent
    enabled: bool
    public_key: str | None = None


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class SubscriptionWrite(BaseModel):
    """What `PushSubscription.toJSON()` gives the page, passed on as it is."""

    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys


class SubscriptionDelete(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class TestResult(BaseModel):
    sent: int = 0
    failed: int = 0


def _weekdays_valid(value: list[int] | None) -> list[int] | None:
    if value is None:
        return value
    days = sorted(set(value))
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("Pick at least one day, Monday 0 to Sunday 6")
    return days


class ReminderWrite(BaseModel):
    kind: ReminderKind
    # Which meal a meal reminder is for; taken from the time when left out
    slot: Slot | None = None
    at: time
    # Monday is 0, as in Python's weekday(); every day by default
    weekdays: list[int] = Field(default=[0, 1, 2, 3, 4, 5, 6], max_length=7)
    enabled: bool = True

    @field_validator("weekdays")
    @classmethod
    def _days(cls, value: list[int]) -> list[int] | None:
        return _weekdays_valid(value)


class ReminderPatch(BaseModel):
    slot: Slot | None = None
    at: time | None = None
    weekdays: list[int] | None = Field(default=None, max_length=7)
    enabled: bool | None = None

    @field_validator("weekdays")
    @classmethod
    def _days(cls, value: list[int] | None) -> list[int] | None:
        return _weekdays_valid(value)


class ReminderRead(BaseModel):
    id: uuid.UUID
    kind: ReminderKind
    slot: Slot | None = None
    at: time
    weekdays: list[int]
    enabled: bool
    last_sent_on: date | None = None
