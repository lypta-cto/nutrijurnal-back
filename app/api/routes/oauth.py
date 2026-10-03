"""
Google sign-in, server side.

Flow:
  1. Browser hits  GET /api/v1/auth/google/authorize  → 302 to Google
  2. User approves → Google redirects to  /api/v1/auth/google/callback?code=…
  3. We swap the code for the user's profile, create or link the account,
     set the refresh cookie, and redirect to the frontend.
  4. The frontend calls POST /api/v1/auth/refresh to get its access token.

The access token is never put in the redirect URL: query strings end up in
browser history and server logs.
"""

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.services import auth as auth_service

router = APIRouter(prefix="/auth/google", tags=["auth"])

oauth = OAuth()

if settings.google_enabled:
    oauth.register(
        name="google",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _require_enabled() -> None:
    if not settings.google_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in your environment.",
        )


@router.get("/authorize")
async def google_authorize(request: Request):
    """Kick off the flow. Point a plain <a href> at this — not fetch()."""
    _require_enabled()
    return await oauth.google.authorize_redirect(request, settings.google_redirect_uri)


@router.get("/callback")
async def google_callback(request: Request):
    _require_enabled()

    from app.core.database import SessionLocal

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=google_denied")

    claims = token.get("userinfo") or {}
    google_sub = claims.get("sub")
    email = (claims.get("email") or "").lower()

    if not google_sub or not email:
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=google_no_email")

    if not claims.get("email_verified", False):
        # Without this check, anyone able to create an unverified Google account
        # for your address could take over the matching local account below.
        return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=google_unverified")

    async with SessionLocal() as session:
        user = await auth_service.get_user_by_google_sub(session, google_sub)

        if user is None:
            # Same person signing in a different way — link, don't duplicate
            user = await auth_service.get_user_by_email(session, email)

            if user is None:
                user = await auth_service.create_user(
                    session,
                    email=email,
                    full_name=claims.get("name"),
                    avatar_url=claims.get("picture"),
                    google_sub=google_sub,
                    is_verified=True,
                )
            else:
                user.google_sub = google_sub
                user.is_verified = True
                if not user.avatar_url:
                    user.avatar_url = claims.get("picture")

        if not user.is_active:
            await session.commit()
            return RedirectResponse(f"{settings.FRONTEND_URL}/login?error=account_disabled")

        raw_refresh = await auth_service.issue_refresh_token(
            session,
            user,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        await session.commit()

    response = RedirectResponse(f"{settings.FRONTEND_URL}/auth/callback")
    auth_service.set_refresh_cookie(response, raw_refresh)
    return response
