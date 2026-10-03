"""
"Try the demo": one tap from the login screen into a diary that already has
two weeks in it — and, if the person likes it, keeping it as their own.
"""

import time as clock
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.security import hash_password
from app.schemas.auth import AuthResponse, DemoClaim, DemoRequest
from app.schemas.user import UserRead
from app.services import auth as auth_service
from app.services import demo

router = APIRouter(prefix="/auth", tags=["auth"])

# When each address last started a demo — in memory, per worker: enough to
# stop the button being used to fill the database, without a store of its own
_started: dict[str, deque[float]] = defaultdict(deque)


def _allow(address: str) -> bool:
    now = clock.monotonic()
    recent = _started[address]
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= settings.DEMO_PER_HOUR:
        return False
    recent.append(now)
    return True


@router.post("/demo", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def start_demo(
    payload: DemoRequest, request: Request, response: Response, session: SessionDep
) -> AuthResponse:
    """A throwaway account, signed in, with two weeks of generic meals, water
    and weight in it. It is deleted after DEMO_TTL_DAYS unless it is kept."""
    if not settings.DEMO_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No demo here")
    address = request.client.host if request.client else "unknown"
    if not _allow(address):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many demos from here — try again in an hour, or create an account",
        )

    user = await demo.create_demo(session, payload.today)

    agent = request.headers.get("user-agent")
    raw_refresh = await auth_service.issue_refresh_token(
        session, user, user_agent=agent, ip_address=address
    )
    auth_service.set_refresh_cookie(response, raw_refresh)
    access_token, expires_in = auth_service.build_access_token(user)
    return AuthResponse(
        access_token=access_token, expires_in=expires_in, user=UserRead.model_validate(user)
    )


@router.post("/demo/claim", response_model=UserRead)
async def keep_demo(payload: DemoClaim, session: SessionDep, user: CurrentUser) -> UserRead:
    """The demo becomes an ordinary account — the diary in it included."""
    if not user.is_demo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This is not a demo account"
        )
    if await auth_service.get_user_by_email(session, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )
    user.email = payload.email.lower()
    user.full_name = payload.full_name
    user.hashed_password = hash_password(payload.password)
    user.is_demo = False
    user.demo_expires_at = None
    await session.flush()
    return UserRead.model_validate(user)
