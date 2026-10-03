"""The doors anyone can knock on — signing in, signing up — let only so many
knocks through, from one address and (for a wrong password) for one email."""

import pytest
from httpx import AsyncClient

from app.core import rate_limit
from app.core.config import settings
from tests.helpers import PASSWORD, PREFIX, sign_up

LOGIN = f"{PREFIX}/auth/login"
REGISTER = f"{PREFIX}/auth/register"


@pytest.fixture
def clock(monkeypatch) -> dict:
    now = {"now": 1000.0}
    monkeypatch.setattr(rate_limit.clock, "monotonic", lambda: now["now"])
    return now


async def _sign_in(client: AsyncClient, email: str, password: str = PASSWORD, **headers):
    return await client.post(LOGIN, json={"email": email, "password": password}, headers=headers)


async def test_wrong_passwords_for_one_email_are_stopped_for_a_while(
    client: AsyncClient, monkeypatch, clock
):
    monkeypatch.setattr(settings, "LOGIN_FAILURES_PER_EMAIL", 3)
    await sign_up(client, "guessed@example.com")
    await sign_up(client, "someone@example.com")

    for _ in range(3):
        assert (await _sign_in(client, "guessed@example.com", "wrong-guess")).status_code == 401

    # Not even the right password gets an answer now — a guesser learns nothing
    refused = await _sign_in(client, "GUESSED@example.com")
    assert refused.status_code == 429
    assert refused.json()["detail"] == (
        "Too many wrong passwords for this email — try again in a few minutes"
    )
    assert 0 < int(refused.headers["retry-after"]) <= 15 * 60
    # Another account from the same address is not held up by it
    assert (await _sign_in(client, "someone@example.com")).status_code == 200

    clock["now"] += 15 * 60 + 1
    assert (await _sign_in(client, "guessed@example.com")).status_code == 200


async def test_signing_in_rightly_never_counts_against_the_account(
    client: AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "LOGIN_FAILURES_PER_EMAIL", 2)
    await sign_up(client, "often@example.com")

    for _ in range(5):
        assert (await _sign_in(client, "often@example.com")).status_code == 200


async def test_one_address_gets_only_so_many_sign_ins(client: AsyncClient, monkeypatch, clock):
    monkeypatch.setattr(settings, "LOGIN_PER_ADDRESS", 3)
    await sign_up(client, "busy@example.com")

    for email in ("busy@example.com", "nobody@example.com", "busy@example.com"):
        assert (await _sign_in(client, email)).status_code in (200, 401)

    refused = await _sign_in(client, "busy@example.com")
    assert refused.status_code == 429
    assert refused.json()["detail"] == (
        "Too many sign-in attempts from here — try again in a few minutes"
    )

    clock["now"] += 15 * 60 + 1
    assert (await _sign_in(client, "busy@example.com")).status_code == 200


async def test_one_address_opens_only_so_many_accounts_an_hour(
    client: AsyncClient, monkeypatch, clock
):
    monkeypatch.setattr(settings, "REGISTER_PER_HOUR", 2)
    body = {"password": PASSWORD, "full_name": "Ana"}

    for n in range(2):
        response = await client.post(REGISTER, json={**body, "email": f"new{n}@example.com"})
        assert response.status_code == 201

    refused = await client.post(REGISTER, json={**body, "email": "new2@example.com"})
    assert refused.status_code == 429
    assert refused.json()["detail"] == "Too many new accounts from here — try again in an hour"

    clock["now"] += 60 * 60 + 1
    later = await client.post(REGISTER, json={**body, "email": "new2@example.com"})
    assert later.status_code == 201


async def test_a_forwarded_address_is_believed_only_behind_a_trusted_proxy(
    client: AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "LOGIN_PER_ADDRESS", 1)
    await sign_up(client, "proxied@example.com")

    # Nobody vouches for the header: writing a new one each time changes nothing
    assert (
        await _sign_in(client, "proxied@example.com", **{"X-Forwarded-For": "1.1.1.1"})
    ).status_code == 200
    spoofed = await _sign_in(client, "proxied@example.com", **{"X-Forwarded-For": "2.2.2.2"})
    assert spoofed.status_code == 429

    # Behind the trusted proxy the last address is the one it saw; whatever
    # the client wrote in front of it is still its own invention
    rate_limit.reset()
    monkeypatch.setattr(settings, "TRUSTED_PROXY", True)
    first = await _sign_in(client, "proxied@example.com", **{"X-Forwarded-For": "9.9.9.9, 3.3.3.3"})
    again = await _sign_in(client, "proxied@example.com", **{"X-Forwarded-For": "8.8.8.8, 3.3.3.3"})
    elsewhere = await _sign_in(client, "proxied@example.com", **{"X-Forwarded-For": "4.4.4.4"})
    assert (first.status_code, again.status_code, elsewhere.status_code) == (200, 429, 200)
