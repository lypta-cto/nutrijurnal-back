"""
Everything that touches users and sessions lives here, so the route handlers
stay thin and the same logic is reachable from tests, the CLI and the OAuth
callback alike.
"""

import uuid
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import Role, User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_google_sub(session: AsyncSession, sub: str) -> User | None:
    result = await session.execute(select(User).where(User.google_sub == sub))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = None,
    full_name: str | None = None,
    role: Role = Role.MEMBER,
    google_sub: str | None = None,
    avatar_url: str | None = None,
    is_verified: bool = False,
) -> User:
    user = User(
        email=email.lower(),
        full_name=full_name,
        hashed_password=hash_password(password) if password else None,
        role=role,
        google_sub=google_sub,
        avatar_url=avatar_url,
        is_verified=is_verified,
    )
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(session, email)

    # Password check runs even when the user is missing, so a wrong email and a
    # wrong password take the same time and can't be told apart by timing.
    if user is None or not user.hashed_password:
        hash_password(password)
        return None

    if not verify_password(password, user.hashed_password):
        return None

    return user if user.is_active else None


async def issue_refresh_token(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> str:
    """Creates a session row and returns the raw token for the cookie."""
    raw, token_hash = generate_refresh_token()

    session.add(
        RefreshToken(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=refresh_token_expiry(),
            user_agent=user_agent[:512] if user_agent else None,
            ip_address=ip_address,
        )
    )
    await session.flush()
    return raw


async def rotate_refresh_token(
    session: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str] | None:
    """
    Swaps a valid refresh token for a fresh one.

    Rotating on every use means a stolen token is only good until the real user
    refreshes — after that the thief's copy is revoked.
    """
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
    )
    stored = result.scalar_one_or_none()

    if stored is None or not stored.is_valid:
        return None

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        return None

    stored.revoked_at = datetime.now(UTC)
    new_raw = await issue_refresh_token(session, user, user_agent=user_agent, ip_address=ip_address)
    return user, new_raw


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> None:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
    )
    stored = result.scalar_one_or_none()

    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Signs the user out everywhere — use after a password change."""
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    tokens = list(result.scalars())

    for token in tokens:
        token.revoked_at = datetime.now(UTC)

    return len(tokens)


def build_access_token(user: User) -> tuple[str, int]:
    token = create_access_token(subject=str(user.id), role=user.role.value)
    return token, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def set_refresh_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        # Scoped to the auth routes so it isn't sent with every API call
        path=f"{settings.API_V1_PREFIX}/auth",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path=f"{settings.API_V1_PREFIX}/auth",
    )
