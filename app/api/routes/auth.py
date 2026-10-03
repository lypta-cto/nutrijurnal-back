from typing import Annotated

from fastapi import APIRouter, Cookie, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.rate_limit import Limiter, client_address, too_many
from app.core.security import hash_password, verify_password
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    MessageResponse,
    ProvidersRead,
    RegisterRequest,
)
from app.schemas.user import PasswordChange, UserRead, UserUpdate
from app.services import auth as auth_service
from app.services import media as media_service

router = APIRouter(prefix="/auth", tags=["auth"])

RefreshCookie = Annotated[str | None, Cookie(alias=settings.REFRESH_COOKIE_NAME)]


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    return request.headers.get("user-agent"), client_address(request)


# Every attempt from one address counts; for one account only the wrong
# passwords do, so its owner signing in on a new phone is never in the way
sign_ins = Limiter(window=15 * 60, limit=lambda: settings.LOGIN_PER_ADDRESS)
wrong_passwords = Limiter(window=15 * 60, limit=lambda: settings.LOGIN_FAILURES_PER_EMAIL)
sign_ups = Limiter(window=60 * 60, limit=lambda: settings.REGISTER_PER_HOUR)


@router.get("/providers", response_model=ProvidersRead)
async def providers() -> ProvidersRead:
    """Public: the login and register screens need it before anyone is signed in."""
    return ProvidersRead(google=settings.google_enabled)


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> AuthResponse:
    address = client_address(request)
    if not sign_ups.take(address):
        raise too_many(sign_ups, address, "Too many new accounts from here — try again in an hour")
    if await auth_service.get_user_by_email(session, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    user = await auth_service.create_user(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
    )

    user_agent, ip = _client_meta(request)
    raw_refresh = await auth_service.issue_refresh_token(
        session, user, user_agent=user_agent, ip_address=ip
    )
    auth_service.set_refresh_cookie(response, raw_refresh)

    access_token, expires_in = auth_service.build_access_token(user)
    return AuthResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> AuthResponse:
    address, email = client_address(request), payload.email.lower()
    if not sign_ins.take(address):
        raise too_many(
            sign_ins, address, "Too many sign-in attempts from here — try again in a few minutes"
        )
    # Refused before the password is checked, so guessing on gets no answer at all
    if not wrong_passwords.allows(email):
        raise too_many(
            wrong_passwords,
            email,
            "Too many wrong passwords for this email — try again in a few minutes",
        )

    user = await auth_service.authenticate(session, payload.email, payload.password)

    if user is None:
        wrong_passwords.hit(email)
        # Deliberately vague: don't reveal whether the email exists
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    user_agent, ip = _client_meta(request)
    raw_refresh = await auth_service.issue_refresh_token(
        session, user, user_agent=user_agent, ip_address=ip
    )
    auth_service.set_refresh_cookie(response, raw_refresh)

    access_token, expires_in = auth_service.build_access_token(user)
    return AuthResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    refresh_token: RefreshCookie = None,
) -> AuthResponse | JSONResponse:
    """Exchanges the refresh cookie for a new access token, rotating the cookie."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )

    user_agent, ip = _client_meta(request)
    rotated = await auth_service.rotate_refresh_token(
        session, refresh_token, user_agent=user_agent, ip_address=ip
    )

    if rotated is None:
        # Answered as a response of its own: a raised HTTPException gets a
        # fresh response and drops the Set-Cookie that clears the dead token,
        # which the browser would otherwise keep sending until it expires
        refused = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired refresh token"},
        )
        auth_service.clear_refresh_cookie(refused)
        return refused

    user, new_raw = rotated
    auth_service.set_refresh_cookie(response, new_raw)

    access_token, expires_in = auth_service.build_access_token(user)
    return AuthResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    response: Response,
    session: SessionDep,
    refresh_token: RefreshCookie = None,
) -> MessageResponse:
    if refresh_token:
        await auth_service.revoke_refresh_token(session, refresh_token)

    auth_service.clear_refresh_cookie(response)
    return MessageResponse(message="Signed out")


@router.get("/me", response_model=UserRead)
async def read_me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", response_model=UserRead)
async def update_me(payload: UserUpdate, user: CurrentUser, session: SessionDep) -> UserRead:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)

    await session.flush()
    return UserRead.model_validate(user)


@router.delete("/me", response_model=MessageResponse)
async def delete_me(user: CurrentUser, session: SessionDep, response: Response) -> MessageResponse:
    """Closes the account for good: the diary, the recipes and the foods this
    person added all go with it (every one of those tables cascades on the
    user). The shared foods stay — they never belonged to anyone."""
    media_service.remove_previous(user.id)
    await session.delete(user)
    await session.flush()
    auth_service.clear_refresh_cookie(response)
    return MessageResponse(message="Account deleted")


@router.post("/me/avatar", response_model=UserRead)
async def upload_avatar(
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> UserRead:
    """Validates and re-encodes the image, then points the user at the result."""
    user.avatar_url = await media_service.store_avatar(file, user.id)
    await session.flush()
    return UserRead.model_validate(user)


@router.delete("/me/avatar", response_model=UserRead)
async def delete_avatar(user: CurrentUser, session: SessionDep) -> UserRead:
    media_service.remove_previous(user.id)
    user.avatar_url = None
    await session.flush()
    return UserRead.model_validate(user)


@router.post("/me/password", response_model=MessageResponse)
async def change_password(
    payload: PasswordChange,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> MessageResponse:
    if not user.hashed_password or not verify_password(
        payload.current_password, user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    user.hashed_password = hash_password(payload.new_password)

    # A password change should end every other session
    await auth_service.revoke_all_sessions(session, user.id)
    auth_service.clear_refresh_cookie(response)

    return MessageResponse(message="Password updated — please sign in again")


@router.get("/sessions", response_model=list[dict])
async def list_sessions(user: CurrentUser) -> list[dict]:
    """Active refresh tokens, so a Settings screen can show 'where I'm signed in'."""
    return [
        {
            "id": str(token.id),
            "user_agent": token.user_agent,
            "ip_address": token.ip_address,
            "created_at": token.created_at.isoformat(),
            "expires_at": token.expires_at.isoformat(),
        }
        for token in user.refresh_tokens
        if token.is_valid
    ]


@router.delete("/sessions", response_model=MessageResponse)
async def revoke_sessions(
    user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> MessageResponse:
    count = await auth_service.revoke_all_sessions(session, user.id)
    auth_service.clear_refresh_cookie(response)
    return MessageResponse(message=f"Revoked {count} session(s)")
