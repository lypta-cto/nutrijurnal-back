"""
Small maintenance commands.

    python -m app.cli dev       # run the API on the port the frontend expects
    python -m app.cli seed      # load (or refresh) the shared foods
    python -m app.cli owner     # create the operator account from FIRST_SUPERUSER_*
    python -m app.cli secret    # print a SECRET_KEY you can paste into .env
"""

import asyncio
import secrets
import sys

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models.user import Role
from app.services import auth as auth_service
from app.services import eating_seed


async def seed() -> None:
    """Keyed by `foods_seed.json`'s slugs, so running it twice changes nothing
    the second time — and running it after editing the file updates in place."""
    async with SessionLocal() as session:
        touched = await eating_seed.seed_foods(session)
        await session.commit()

    print(f"✓ {touched} shared foods in the pantry")
    await engine.dispose()


async def owner() -> None:
    async with SessionLocal() as session:
        existing = await auth_service.get_user_by_email(session, settings.FIRST_SUPERUSER_EMAIL)

        if existing is not None:
            print(f"✓ {existing.email} already exists ({existing.role.value})")
            await engine.dispose()
            return

        user = await auth_service.create_user(
            session,
            email=settings.FIRST_SUPERUSER_EMAIL,
            password=settings.FIRST_SUPERUSER_PASSWORD,
            full_name="Owner",
            role=Role.OWNER,
            is_verified=True,
        )
        await session.commit()

        print(f"✓ Created {user.email} with the owner role")
        print("  Change the password after signing in.")

    await engine.dispose()


def dev() -> None:
    """Reads the port from settings, so plain `uvicorn app.main:app` can't
    silently start on 8000 and leave the frontend talking to nothing."""
    import uvicorn

    print(f"→ http://{settings.API_HOST}:{settings.API_PORT}  (docs at /docs)")
    uvicorn.run(
        "app.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True,
    )


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "dev":
        dev()
    elif command == "seed":
        asyncio.run(seed())
    elif command == "owner":
        asyncio.run(owner())
    elif command == "secret":
        print(secrets.token_urlsafe(48))
    else:
        print(__doc__)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
