"""
Reminders, delivered as Web Push notifications.

A `PushSubscription` is one browser (one phone, one laptop) that agreed to
receive notifications; a `Reminder` is what a person asked to be reminded of
and when, in their own clock. The scheduler in services/reminders.py sends
what is due to every subscription the person has.
"""

import uuid
from datetime import date, time

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin

# Monday is bit 0, as in datetime.weekday(); every day is all seven bits
EVERY_DAY = 0b1111111


class PushSubscription(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "push_subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The push service's address for this browser — unique: one browser
    # belongs to whoever subscribed it last
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512))


class Reminder(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "reminders"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # meal · water · summary
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # Which meal, for a meal reminder
    slot: Mapped[str | None] = mapped_column(String(10))
    at: Mapped[time] = mapped_column(Time, nullable=False)
    weekdays: Mapped[int] = mapped_column(Integer, nullable=False, default=EVERY_DAY)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The person's own date it last went out on — so it goes out once a day,
    # however many workers are running the loop
    last_sent_on: Mapped[date | None] = mapped_column(Date)
