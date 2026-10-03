"""What a person is told when the database says no: a write that clashed
with what is already saved is a conflict, not an outage, and the hint for
whoever runs the API locally never reaches the public."""

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.routes import body
from app.core.config import settings
from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"


def _failing(error: Exception):
    async def fail(*_args, **_kwargs):
        raise error

    return fail


async def test_a_write_that_breaks_a_constraint_is_a_conflict(client: AsyncClient, monkeypatch):
    headers = await auth_headers(client, "clash@example.com")
    clash = IntegrityError("INSERT INTO weight_entries …", {}, Exception("duplicate key"))
    monkeypatch.setattr(body, "latest_weight", _failing(clash))

    response = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 70}, headers=headers)

    assert response.status_code == 409
    assert "duplicate" not in response.text and "INSERT" not in response.text


@pytest.mark.parametrize(
    ("environment", "hinted"), [("local", True), ("staging", False), ("production", False)]
)
async def test_a_database_that_is_down_is_explained_for_who_is_reading(
    client: AsyncClient, monkeypatch, environment, hinted
):
    headers = await auth_headers(client, f"down-{environment}@example.com")
    down = OperationalError("SELECT 1", {}, Exception("connection refused on 10.0.0.5"))
    monkeypatch.setattr(body, "latest_weight", _failing(down))
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)

    response = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 70}, headers=headers)

    assert response.status_code == 503
    assert ("docker" in response.json()["detail"]) is hinted
    assert "10.0.0.5" not in response.text
