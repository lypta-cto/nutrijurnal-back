"""
Beside the food: the water drunk and the weight on the scale.

Water is kept as one row per glass, so taking back the last one is deleting
a row, and a day's total is a sum. Weight is one number per person per day —
weighing twice in a morning corrects the first one.
"""

import uuid
from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class WaterEntry(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "water_entries"
    __table_args__ = (Index("ix_water_entries_user_id_day", "user_id", "day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    ml: Mapped[int] = mapped_column(Integer, nullable=False)


class WeightEntry(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "weight_entries"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_weight_entries_user_id_day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kg: Mapped[float] = mapped_column(Float, nullable=False)
