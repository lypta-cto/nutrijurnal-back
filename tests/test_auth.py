import io
import threading
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.models import Food, Meal, Recipe, RefreshToken, Role, User
from app.services import media
from tests.helpers import png_claiming

PREFIX = settings.API_V1_PREFIX
REGISTER = {"email": "user@example.com", "password": "supersecret1", "full_name": "Test User"}


async def register(client: AsyncClient, **overrides) -> dict:
    response = await client.post(f"{PREFIX}/auth/register", json={**REGISTER, **overrides})
    return response.json()


async def test_register_returns_token_and_sets_cookie(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/register", json=REGISTER)

    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["user"]["email"] == REGISTER["email"]
    assert body["user"]["role"] == "member"
    # The refresh token must never be in the body
    assert "refresh_token" not in body
    assert settings.REFRESH_COOKIE_NAME in response.cookies


async def test_anyone_can_sign_up_and_land_signed_in(client: AsyncClient):
    """Open registration: a stranger's email, password and name are enough, and
    the answer is the same session a login hands out."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={"email": "Stranger@Example.com", "password": "eat-the-frog", "full_name": " Ana "},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_type"] == "bearer" and body["expires_in"] > 0
    assert body["user"]["email"] == "stranger@example.com"
    assert body["user"]["full_name"] == "Ana"
    # A new account has not been through the first-run questions yet
    assert body["user"]["onboarded_at"] is None

    me = await client.get(
        f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["email"] == "stranger@example.com"

    # The refresh cookie it set works like one from a login
    refreshed = await client.post(f"{PREFIX}/auth/refresh")
    assert refreshed.status_code == 200


async def test_register_needs_a_name_and_a_real_password(client: AsyncClient):
    nameless = await client.post(
        f"{PREFIX}/auth/register", json={"email": "a@example.com", "password": "supersecret1"}
    )
    blank = await client.post(f"{PREFIX}/auth/register", json={**REGISTER, "full_name": "   "})
    short = await client.post(f"{PREFIX}/auth/register", json={**REGISTER, "password": "short"})

    assert nameless.status_code == blank.status_code == short.status_code == 422


async def test_register_rejects_duplicate_email(client: AsyncClient):
    await register(client)
    response = await client.post(
        f"{PREFIX}/auth/register", json={**REGISTER, "email": REGISTER["email"].upper()}
    )

    assert response.status_code == 409


async def test_providers_say_whether_google_is_on(client: AsyncClient):
    response = await client.get(f"{PREFIX}/auth/providers")

    assert response.status_code == 200
    # No GOOGLE_CLIENT_ID in the test environment, so the button stays hidden
    assert response.json() == {"password": True, "google": False}


async def test_google_sign_in_answers_501_while_it_is_off(client: AsyncClient):
    response = await client.get(f"{PREFIX}/auth/google/authorize")

    assert response.status_code == 501


async def test_login_with_correct_password(client: AsyncClient):
    await register(client)

    response = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": REGISTER["email"], "password": REGISTER["password"]},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_with_wrong_password_is_rejected(client: AsyncClient):
    await register(client)

    response = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": REGISTER["email"], "password": "wrong-password"},
    )

    assert response.status_code == 401
    # Same message as an unknown email, so accounts can't be enumerated
    assert response.json()["detail"] == "Incorrect email or password"


async def test_login_with_unknown_email_is_rejected(client: AsyncClient):
    response = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "nobody@example.com", "password": "supersecret1"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


async def test_me_requires_a_token(client: AsyncClient):
    response = await client.get(f"{PREFIX}/auth/me")

    assert response.status_code == 401


async def test_me_returns_the_current_user(client: AsyncClient):
    body = await register(client)

    response = await client.get(
        f"{PREFIX}/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == REGISTER["email"]


async def test_refresh_rotates_the_cookie(client: AsyncClient):
    await register(client)
    first_cookie = client.cookies[settings.REFRESH_COOKIE_NAME]

    response = await client.post(f"{PREFIX}/auth/refresh")

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert client.cookies[settings.REFRESH_COOKIE_NAME] != first_cookie


async def test_used_refresh_token_cannot_be_replayed(client: AsyncClient):
    await register(client)
    stolen = client.cookies[settings.REFRESH_COOKIE_NAME]

    await client.post(f"{PREFIX}/auth/refresh")  # rotates, revoking `stolen`

    client.cookies.set(settings.REFRESH_COOKIE_NAME, stolen)
    response = await client.post(f"{PREFIX}/auth/refresh")

    assert response.status_code == 401


async def test_refresh_without_a_cookie_is_rejected(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/refresh")

    assert response.status_code == 401


async def test_logout_revokes_the_session(client: AsyncClient):
    await register(client)

    logout = await client.post(f"{PREFIX}/auth/logout")
    assert logout.status_code == 200

    response = await client.post(f"{PREFIX}/auth/refresh")
    assert response.status_code == 401


async def test_members_cannot_reach_admin_routes(client: AsyncClient):
    body = await register(client)

    response = await client.get(
        f"{PREFIX}/users",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 403


async def test_health_is_public(client: AsyncClient):
    response = await client.get("/health")

    # Health probes the real engine on purpose — a monitor wants to know about
    # the actual database, not an injected test session. So the suite asserts
    # it needs no auth and reports its findings, not that the DB happens to be
    # up while the tests run.
    assert response.status_code in (200, 503)

    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert body["database"] in ("ok", "unavailable")


async def test_deleting_the_account_takes_everything_with_it(
    client: AsyncClient, session: AsyncSession
):
    body = await register(client)
    owner_id = uuid.UUID(body["user"]["id"])
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    food = await client.post(
        f"{PREFIX}/eating/foods",
        json={"name": "Moj hleb", "kcal": 250, "protein": 9, "carbs": 48, "fat": 2},
        headers=headers,
    )
    meal = await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": "2026-10-01",
            "title": "Breakfast",
            "items": [{"food_id": food.json()["id"], "quantity": 80, "unit": "g"}],
        },
        headers=headers,
    )
    recipe = await client.post(f"{PREFIX}/eating/recipes", json={"title": "Tost"}, headers=headers)
    assert meal.status_code == recipe.status_code == 201

    gone = await client.delete(f"{PREFIX}/auth/me", headers=headers)
    assert gone.status_code == 200

    # Nothing of theirs is left behind
    for model in (Food, Meal, Recipe):
        left = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == owner_id)
        )
        assert left == 0, model.__name__

    # Signed out everywhere, and the address is free to sign up again
    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401
    again = await client.post(f"{PREFIX}/auth/register", json=REGISTER)
    assert again.status_code == 201


# --- Registration, more closely -----------------------------------------------


async def test_register_validates_every_field(client: AsyncClient):
    cases = {
        "not an email": {**REGISTER, "email": "not-an-email"},
        "seven characters": {**REGISTER, "password": "1234567"},
        "over 128 characters": {**REGISTER, "password": "x" * 129},
        "a name over 255": {**REGISTER, "full_name": "A" * 256},
        "no password": {"email": "a@example.com", "full_name": "A"},
        "no email": {"password": "supersecret1", "full_name": "A"},
    }
    for case, body in cases.items():
        response = await client.post(f"{PREFIX}/auth/register", json=body)
        assert response.status_code == 422, case

    # Eight characters is the floor, not one short of it
    assert (
        await client.post(f"{PREFIX}/auth/register", json={**REGISTER, "password": "12345678"})
    ).status_code == 201


async def test_sign_up_can_not_choose_its_own_role(client: AsyncClient):
    """The body is the public's to write — a role or a verified flag smuggled
    into it must never stick."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={**REGISTER, "role": "owner", "is_verified": True, "is_active": False},
    )

    assert response.status_code == 201
    user = response.json()["user"]
    assert user["role"] == "member"
    assert user["is_verified"] is False and user["is_active"] is True
    assert user["has_password"] is True
    # The hash never leaves the server
    assert "hashed_password" not in user and "password" not in user


async def test_the_refresh_cookie_is_http_only_and_scoped_to_auth(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/register", json=REGISTER)

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{settings.REFRESH_COOKIE_NAME}=")
    assert "httponly" in cookie
    assert f"path={PREFIX}/auth".lower() in cookie
    assert "samesite=lax" in cookie
    assert f"max-age={30 * 24 * 60 * 60}" in cookie


async def test_login_ignores_the_case_of_the_email(client: AsyncClient):
    await register(client)

    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": "USER@Example.COM", "password": "supersecret1"}
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == REGISTER["email"]


async def test_a_deactivated_account_can_not_sign_in_or_carry_on(
    client: AsyncClient, session: AsyncSession
):
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    user = await session.get(User, uuid.UUID(body["user"]["id"]))
    user.is_active = False
    await session.commit()

    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": REGISTER["email"], "password": REGISTER["password"]},
    )
    # The same words as a wrong password: a closed account is not advertised
    assert login.status_code == 401
    assert login.json()["detail"] == "Incorrect email or password"
    assert (await client.get(f"{PREFIX}/auth/me", headers=headers)).status_code == 401
    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401


# --- Access tokens ------------------------------------------------------------


async def test_only_a_live_token_signed_by_this_server_is_accepted(client: AsyncClient):
    body = await register(client)
    subject, role = body["user"]["id"], body["user"]["role"]
    now = datetime.now(UTC)
    claims = {"sub": subject, "role": role, "type": "access", "iat": now}

    forged = {
        "expired": create_access_token(subject, role, expires_delta=timedelta(seconds=-1)),
        "signed elsewhere": jwt.encode(
            {**claims, "exp": now + timedelta(minutes=5)},
            "another-secret-key-that-is-long-enough-too",
            algorithm="HS256",
        ),
        "unsigned": jwt.encode({**claims, "exp": now + timedelta(minutes=5)}, None, "none"),
        "not an access token": jwt.encode(
            {**claims, "type": "refresh", "exp": now + timedelta(minutes=5)},
            settings.SECRET_KEY,
            algorithm="HS256",
        ),
        "nobody": create_access_token(str(uuid.uuid4()), role),
        "garbage": "not.a.token",
    }
    for case, token in forged.items():
        response = await client.get(
            f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401, case

    # A token without the Bearer scheme is no token at all
    raw = await client.get(f"{PREFIX}/auth/me", headers={"Authorization": body["access_token"]})
    assert raw.status_code == 401


async def test_editing_ones_profile_can_not_raise_ones_role(client: AsyncClient):
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    response = await client.patch(
        f"{PREFIX}/auth/me",
        json={"full_name": "Ana Anić", "role": "owner", "is_active": False, "email": "x@y.com"},
        headers=headers,
    )

    assert response.status_code == 200
    me = response.json()
    assert me["full_name"] == "Ana Anić"
    assert (me["role"], me["is_active"], me["email"]) == ("member", True, REGISTER["email"])
    # And it was saved, not just echoed
    again = (await client.get(f"{PREFIX}/auth/me", headers=headers)).json()
    assert again["full_name"] == "Ana Anić" and again["role"] == "member"


# --- Refresh tokens and sessions ----------------------------------------------


async def test_rotation_keeps_going_one_cookie_at_a_time(client: AsyncClient):
    await register(client)
    seen = {client.cookies[settings.REFRESH_COOKIE_NAME]}

    for _ in range(3):
        response = await client.post(f"{PREFIX}/auth/refresh")
        assert response.status_code == 200
        assert response.json()["user"]["email"] == REGISTER["email"]
        seen.add(client.cookies[settings.REFRESH_COOKIE_NAME])

    # Every use handed out a new one
    assert len(seen) == 4


async def _expire_every_refresh_token(session: AsyncSession) -> None:
    await session.execute(
        update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()


async def test_an_expired_or_unknown_refresh_token_is_refused(
    client: AsyncClient, session: AsyncSession
):
    await register(client)
    await _expire_every_refresh_token(session)

    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401

    client.cookies.set(settings.REFRESH_COOKIE_NAME, "made-up", path=f"{PREFIX}/auth")
    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401


async def test_a_refused_refresh_token_is_cleared_from_the_browser(
    client: AsyncClient, session: AsyncSession
):
    await register(client)
    await _expire_every_refresh_token(session)

    refused = await client.post(f"{PREFIX}/auth/refresh")

    assert refused.status_code == 401
    assert refused.json() == {"detail": "Invalid or expired refresh token"}
    assert f'{settings.REFRESH_COOKIE_NAME}=""' in refused.headers.get("set-cookie", "")
    assert f"Path={PREFIX}/auth" in refused.headers["set-cookie"]


async def test_logout_without_a_cookie_still_signs_out(client: AsyncClient):
    response = await client.post(f"{PREFIX}/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"message": "Signed out"}


async def test_sessions_are_listed_and_can_all_be_ended(client: AsyncClient):
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    for agent in ("Phone", "Laptop"):
        await client.post(
            f"{PREFIX}/auth/login",
            json={"email": REGISTER["email"], "password": REGISTER["password"]},
            headers={"User-Agent": agent},
        )

    listed = await client.get(f"{PREFIX}/auth/sessions", headers=headers)
    assert listed.status_code == 200
    agents = {row["user_agent"] for row in listed.json()}
    assert {"Phone", "Laptop"} <= agents and len(listed.json()) == 3

    ended = await client.delete(f"{PREFIX}/auth/sessions", headers=headers)
    assert ended.json()["message"] == "Revoked 3 session(s)"
    assert (await client.get(f"{PREFIX}/auth/sessions", headers=headers)).json() == []
    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401


async def test_sessions_are_each_persons_own(client: AsyncClient):
    mine = await register(client)
    theirs = await register(client, email="other@example.com")

    listed = await client.get(
        f"{PREFIX}/auth/sessions", headers={"Authorization": f"Bearer {theirs['access_token']}"}
    )
    assert len(listed.json()) == 1

    # Ending theirs leaves mine working
    await client.delete(
        f"{PREFIX}/auth/sessions", headers={"Authorization": f"Bearer {theirs['access_token']}"}
    )
    login = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": REGISTER["email"], "password": REGISTER["password"]},
    )
    assert login.status_code == 200
    mine_listed = await client.get(
        f"{PREFIX}/auth/sessions", headers={"Authorization": f"Bearer {mine['access_token']}"}
    )
    assert len(mine_listed.json()) == 2


# --- Password -----------------------------------------------------------------


async def test_changing_the_password_needs_the_old_one_and_ends_every_session(
    client: AsyncClient,
):
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    wrong = await client.post(
        f"{PREFIX}/auth/me/password",
        json={"current_password": "not-it-at-all", "new_password": "brand-new-pass"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "Current password is incorrect"

    short = await client.post(
        f"{PREFIX}/auth/me/password",
        json={"current_password": REGISTER["password"], "new_password": "short"},
        headers=headers,
    )
    assert short.status_code == 422

    changed = await client.post(
        f"{PREFIX}/auth/me/password",
        json={"current_password": REGISTER["password"], "new_password": "brand-new-pass"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert (await client.post(f"{PREFIX}/auth/refresh")).status_code == 401

    old = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": REGISTER["email"], "password": REGISTER["password"]},
    )
    new = await client.post(
        f"{PREFIX}/auth/login", json={"email": REGISTER["email"], "password": "brand-new-pass"}
    )
    assert (old.status_code, new.status_code) == (401, 200)


# --- Avatar -------------------------------------------------------------------


def _png(size=(64, 64), colour=(200, 40, 40, 128)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_an_avatar_is_re_encoded_and_replaces_the_last_one(
    client: AsyncClient, uploads, monkeypatch
):
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    first = await client.post(
        f"{PREFIX}/auth/me/avatar", files={"file": ("me.png", _png(), "image/png")}, headers=headers
    )
    assert first.status_code == 200, first.text
    url = first.json()["avatar_url"]
    assert url.startswith("/uploads/avatars/") and url.endswith(".webp")
    stored = uploads / "avatars" / url.rsplit("/", 1)[-1]
    with Image.open(stored) as image:
        assert image.format == "WEBP" and max(image.size) <= 512

    second = await client.post(
        f"{PREFIX}/auth/me/avatar",
        files={"file": ("big.png", _png((1200, 800)), "image/png")},
        headers=headers,
    )
    assert second.json()["avatar_url"] != url
    # The old file is gone, so only the current one is on disk
    assert [path.name for path in (uploads / "avatars").iterdir()] == [
        second.json()["avatar_url"].rsplit("/", 1)[-1]
    ]

    not_an_image = await client.post(
        f"{PREFIX}/auth/me/avatar",
        files={"file": ("me.png", b"<svg onload=alert(1)>", "image/png")},
        headers=headers,
    )
    assert not_an_image.status_code == 400

    # A few bytes claiming a vast canvas is refused from its header
    for width, height in ((9000, 9000), (100_000, 100_000)):
        bomb = await client.post(
            f"{PREFIX}/auth/me/avatar",
            files={"file": ("bomb.png", png_claiming(width, height), "image/png")},
            headers=headers,
        )
        assert bomb.status_code == 400, (width, bomb.text)

    monkeypatch.setattr(settings, "MAX_AVATAR_BYTES", 1024)
    too_big = await client.post(
        f"{PREFIX}/auth/me/avatar",
        files={"file": ("big.png", _png((600, 600), (1, 2, 3, 255)) + b"\0" * 2048, "image/png")},
        headers=headers,
    )
    assert too_big.status_code == 413

    removed = await client.delete(f"{PREFIX}/auth/me/avatar", headers=headers)
    assert removed.json()["avatar_url"] is None
    assert list((uploads / "avatars").iterdir()) == []


async def test_an_avatar_is_encoded_off_the_event_loop(client: AsyncClient, monkeypatch):
    """Re-encoding a photo is real work; on the event loop it would hold up
    every other person's request while it ran."""
    body = await register(client)
    loop_thread = threading.current_thread()
    encoded_on = []
    real_encode = media._encode

    def spy(raw):
        encoded_on.append(threading.current_thread())
        return real_encode(raw)

    monkeypatch.setattr(media, "_encode", spy)

    response = await client.post(
        f"{PREFIX}/auth/me/avatar",
        files={"file": ("me.png", _png(), "image/png")},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 200, response.text
    assert encoded_on and encoded_on[0] is not loop_thread


# --- Closing an account -------------------------------------------------------


async def test_closing_an_account_leaves_everyone_else_alone(
    client: AsyncClient, session: AsyncSession, uploads
):
    leaving = await register(client)
    staying = await register(client, email="staying@example.com")
    leaving_headers = {"Authorization": f"Bearer {leaving['access_token']}"}
    staying_headers = {"Authorization": f"Bearer {staying['access_token']}"}
    for headers in (leaving_headers, staying_headers):
        await client.post(
            f"{PREFIX}/eating/foods",
            json={"name": "Moj hleb", "kcal": 250, "protein": 9, "carbs": 48, "fat": 2},
            headers=headers,
        )
        await client.post(
            f"{PREFIX}/eating/meals", json={"day": "2026-10-01", "title": "Lunch"}, headers=headers
        )
    await client.post(
        f"{PREFIX}/auth/me/avatar",
        files={"file": ("me.png", _png(), "image/png")},
        headers=leaving_headers,
    )

    assert (await client.delete(f"{PREFIX}/auth/me", headers=leaving_headers)).status_code == 200

    # Their token dies with the account, and so does their avatar file
    assert (await client.get(f"{PREFIX}/auth/me", headers=leaving_headers)).status_code == 401
    assert list((uploads / "avatars").iterdir()) == []
    # The other person still has everything
    day = (await client.get(f"{PREFIX}/eating/days/2026-10-01", headers=staying_headers)).json()
    assert [meal["title"] for meal in day["meals"]] == ["Lunch"]
    mine = await client.get(
        f"{PREFIX}/eating/foods", params={"mine": True}, headers=staying_headers
    )
    assert [food["name"] for food in mine.json()] == ["Moj hleb"]
    assert await session.scalar(select(func.count()).select_from(User)) == 1


# --- Operator routes ----------------------------------------------------------


async def test_the_user_admin_is_for_admins_only(client: AsyncClient, session: AsyncSession):
    member = await register(client)
    member_headers = {"Authorization": f"Bearer {member['access_token']}"}
    for request in (
        client.get(f"{PREFIX}/users", headers=member_headers),
        client.get(f"{PREFIX}/users/me", headers=member_headers),
        client.get(f"{PREFIX}/users/{member['user']['id']}", headers=member_headers),
        client.post(
            f"{PREFIX}/users",
            json={"email": "x@example.com", "password": "supersecret1", "role": "admin"},
            headers=member_headers,
        ),
        client.delete(f"{PREFIX}/users/{member['user']['id']}", headers=member_headers),
    ):
        assert (await request).status_code == 403

    admin = await register(client, email="admin@example.com")
    row = await session.get(User, uuid.UUID(admin["user"]["id"]))
    row.role = Role.ADMIN
    await session.commit()
    # The role rides in the token, so the admin signs in again to carry it
    login = await client.post(
        f"{PREFIX}/auth/login", json={"email": "admin@example.com", "password": "supersecret1"}
    )
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    listed = await client.get(f"{PREFIX}/users", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 2
    # A page number past any real list is refused, not sent to the database as an offset
    far = await client.get(f"{PREFIX}/users", params={"page": 10**12}, headers=admin_headers)
    assert far.status_code == 422
    own = await client.delete(f"{PREFIX}/users/{admin['user']['id']}", headers=admin_headers)
    assert own.status_code == 400
