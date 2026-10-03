import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.refresh_token import RefreshToken


class Role(enum.StrEnum):
    """Ranked: every role implies the permissions of the ones before it.

    Mirrors the frontend's `can(role)` helper — keep the two in step.
    """

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _ROLE_ORDER.index(self)

    def can(self, minimum: "Role") -> bool:
        return self.rank >= minimum.rank


_ROLE_ORDER: list[Role] = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(1024))

    # Null for accounts that only ever signed in through an OAuth provider
    hashed_password: Mapped[str | None] = mapped_column(String(255))

    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda enum_cls: [m.value for m in enum_cls]),
        default=Role.MEMBER,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Set when the account is linked to Google
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)

    # --- The diary's daily targets -------------------------------------------
    # Every one is optional: a day with no target is still counted, it just
    # has nothing to be measured against.
    target_kcal: Mapped[int | None] = mapped_column(Integer)
    target_protein: Mapped[int | None] = mapped_column(Integer)
    target_carbs: Mapped[int | None] = mapped_column(Integer)
    target_fat: Mapped[int | None] = mapped_column(Integer)
    # When the first-run questions were answered (or skipped). Null sends the
    # frontend to /onboarding, so a new account never lands on an empty diary
    # with no idea what it is measuring against.
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- What the targets were worked out from --------------------------------
    # The calculator's answers, kept so Settings can reopen it where it was
    # left. All optional: someone who types the numbers in by hand never has
    # to say how old they are.
    sex: Mapped[str | None] = mapped_column(String(8))  # female · male · other
    birth_year: Mapped[int | None] = mapped_column(Integer)
    height_cm: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    activity: Mapped[str | None] = mapped_column(String(12))
    goal: Mapped[str | None] = mapped_column(String(8))  # lose · maintain · gain
    goal_pace: Mapped[float | None] = mapped_column(Float)  # kg a week
    protein_per_kg: Mapped[float | None] = mapped_column(Float)
    fat_percent: Mapped[int | None] = mapped_column(Integer)

    # --- Water ------------------------------------------------------------------
    # Null reads as the defaults in app/api/routes/body.py (2 l, 250 ml glasses)
    water_goal_ml: Mapped[int | None] = mapped_column(Integer)
    water_glass_ml: Mapped[int | None] = mapped_column(Integer)

    # IANA name ("Europe/Belgrade"), sent by the browser — reminders are set
    # in the person's own clock, not the server's
    timezone: Mapped[str | None] = mapped_column(String(64))

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def has_password(self) -> bool:
        return self.hashed_password is not None

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"
