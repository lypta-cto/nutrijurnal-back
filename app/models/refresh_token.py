import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(UUIDMixin, TimestampMixin, Base):
    """
    One row per issued refresh token.

    Storing them lets `logout` actually revoke a session and lets `refresh`
    rotate: the old token is revoked the moment a new one is handed out, so a
    stolen token stops working as soon as the real user refreshes.
    """

    __tablename__ = "refresh_tokens"

    # SHA-256 of the raw token — the raw value only ever exists in the cookie
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Rough audit trail — handy when someone asks "where am I signed in?"
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    @property
    def is_valid(self) -> bool:
        if self.revoked_at is not None:
            return False

        # Postgres hands back an aware datetime, SQLite a naive one. Everything
        # is written as UTC, so treating a naive value as UTC is correct.
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        return expires_at > datetime.now(UTC)
