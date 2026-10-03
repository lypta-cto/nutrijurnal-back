from httpx import AsyncClient

from app.core.config import settings

PREFIX = settings.API_V1_PREFIX


async def auth_headers(client: AsyncClient, email: str, full_name: str = "Test User") -> dict:
    """Signs a new person up and hands back the header their requests carry."""
    response = await client.post(
        f"{PREFIX}/auth/register",
        json={"email": email, "password": "supersecret1", "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
