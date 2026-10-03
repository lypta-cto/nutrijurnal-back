import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Food, Meal, Recipe

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
