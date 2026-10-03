"""Undo for what was deleted, and reminders delivered by Web Push: kept per
person, sent once a day in the person's own clock, quiet when there is
nothing to remind about."""

import base64
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import DeletedMeal, PushSubscription, Reminder
from app.models.push import EVERY_DAY
from app.services import push, reminders
from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {"p256dh": "BBrowsersPublicKeyStandIn", "auth": "AuthSecretStandIn"}


async def _meal(client: AsyncClient, headers: dict, **body) -> dict:
    response = await client.post(
        f"{PREFIX}/eating/meals", json={"day": DAY, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Undo ----------------------------------------------------------------------


async def test_a_deleted_meal_comes_back_whole_with_undo(client: AsyncClient):
    headers = await auth_headers(client, "undo@example.com")
    original = await _meal(
        client,
        headers,
        at="12:30",
        slot="lunch",
        title="Ručak",
        note="kod kuće",
        items=[
            {"label": "Pasulj", "macros": {"kcal": 420, "protein": 22, "carbs": 50, "fat": 12}},
            {"label": "Hleb", "quantity": 2, "macros": {"kcal": 80, "carbs": 15}},
        ],
    )
    path = f"{PREFIX}/eating/meals/{original['id']}"

    assert (await client.delete(path, headers=headers)).status_code == 204
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["meals"] == []

    restored = await client.post(f"{path}/restore", headers=headers)

    assert restored.status_code == 200, restored.text
    body = restored.json()
    for field in ("id", "day", "at", "slot", "title", "note", "kcal", "protein"):
        assert body[field] == original[field], field
    assert [item["id"] for item in body["items"]] == [item["id"] for item in original["items"]]
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert [meal["id"] for meal in day["meals"]] == [original["id"]]

    # Undone once; there is nothing left to undo
    assert (await client.post(f"{path}/restore", headers=headers)).status_code == 404


async def test_a_restored_meal_keeps_its_recording(client: AsyncClient):
    headers = await auth_headers(client, "undo-voice@example.com")
    meal = await _meal(client, headers, title="Rekao sam", items=[])
    path = f"{PREFIX}/eating/meals/{meal['id']}"
    await client.post(
        f"{path}/voice",
        files={"file": ("voice.webm", b"RIFF-not-really-audio", "audio/webm")},
        data={"seconds": "3.2"},
        headers=headers,
    )

    await client.delete(path, headers=headers)
    restored = (await client.post(f"{path}/restore", headers=headers)).json()

    assert restored["has_voice"] is True and restored["voice_seconds"] == 3.2
    played = await client.get(f"{path}/voice", headers=headers)
    assert played.content == b"RIFF-not-really-audio"


async def test_nobody_can_undo_someone_elses_delete(client: AsyncClient):
    owner = await auth_headers(client, "undo-owner@example.com")
    stranger = await auth_headers(client, "undo-stranger@example.com")
    meal = await _meal(client, owner, items=[{"label": "Kafa", "macros": {"kcal": 5}}])
    path = f"{PREFIX}/eating/meals/{meal['id']}"
    await client.delete(path, headers=owner)

    assert (await client.post(f"{path}/restore", headers=stranger)).status_code == 404
    assert (await client.post(f"{path}/restore", headers=owner)).status_code == 200


async def test_a_removed_line_goes_back_in_its_place(client: AsyncClient):
    headers = await auth_headers(client, "undo-item@example.com")
    meal = await _meal(
        client,
        headers,
        items=[
            {"label": "Prvo", "macros": {"kcal": 100}},
            {"label": "Drugo", "macros": {"kcal": 200}},
            {"label": "Treće", "macros": {"kcal": 300}},
        ],
    )
    path = f"{PREFIX}/eating/meals/{meal['id']}/items"
    middle = meal["items"][1]
    await client.delete(f"{path}/{middle['id']}", headers=headers)

    back = await client.post(
        path,
        json={"label": "Drugo", "quantity": 1, "macros": {"kcal": 200}, "position": 1},
        headers=headers,
    )

    assert [item["label"] for item in back.json()["items"]] == ["Prvo", "Drugo", "Treće"]
    assert back.json()["kcal"] == 600


async def test_deleted_meals_are_kept_a_day_then_purged(client: AsyncClient, session: AsyncSession):
    headers = await auth_headers(client, "purge@example.com")
    meal = await _meal(client, headers, items=[{"label": "Burek", "macros": {"kcal": 700}}])
    await client.delete(f"{PREFIX}/eating/meals/{meal['id']}", headers=headers)

    await reminders.tidy(datetime.now(UTC))
    assert await session.scalar(select(func.count()).select_from(DeletedMeal)) == 1

    await reminders.tidy(datetime(2099, 1, 1, tzinfo=UTC))
    assert await session.scalar(select(func.count()).select_from(DeletedMeal)) == 0


# --- When a reminder is due ---------------------------------------------------


def _reminder(at: str, **fields) -> Reminder:
    return Reminder(
        kind=fields.get("kind", "meal"),
        slot=fields.get("slot", "lunch"),
        at=time.fromisoformat(at),
        weekdays=fields.get("weekdays", EVERY_DAY),
        enabled=fields.get("enabled", True),
        last_sent_on=fields.get("last_sent_on"),
    )


BELGRADE = ZoneInfo("Europe/Belgrade")


def test_a_reminder_is_due_in_its_owners_own_clock():
    # 11:05 UTC is 13:05 in Belgrade in September
    now = datetime(2026, 9, 21, 11, 5, tzinfo=UTC)

    assert reminders.due_on(_reminder("13:00"), now, BELGRADE) == date(2026, 9, 21)
    assert reminders.due_on(_reminder("13:00"), now, ZoneInfo("UTC")) is None


def test_a_reminder_waits_for_its_time_and_gives_up_after_the_grace():
    early = datetime(2026, 9, 21, 10, 59, tzinfo=UTC)
    late = datetime(2026, 9, 21, 11, 31, tzinfo=UTC)

    assert reminders.due_on(_reminder("13:00"), early, BELGRADE) is None
    assert reminders.due_on(_reminder("13:00"), late, BELGRADE) is None


def test_a_reminder_goes_out_once_a_day_on_its_days_only():
    now = datetime(2026, 9, 21, 11, 5, tzinfo=UTC)  # a Monday
    sent_today = _reminder("13:00", last_sent_on=date(2026, 9, 21))
    weekends = _reminder("13:00", weekdays=reminders.mask_of([5, 6]))
    off = _reminder("13:00", enabled=False)

    assert reminders.due_on(sent_today, now, BELGRADE) is None
    assert reminders.due_on(weekends, now, BELGRADE) is None
    assert reminders.due_on(off, now, BELGRADE) is None
    assert reminders.weekdays_of(reminders.mask_of([0, 2, 4])) == [0, 2, 4]


# --- Reminders, per person ----------------------------------------------------


async def test_reminders_are_kept_per_person(client: AsyncClient):
    mine = await auth_headers(client, "reminders@example.com")
    theirs = await auth_headers(client, "reminders-other@example.com")

    created = await client.post(
        f"{PREFIX}/reminders", json={"kind": "meal", "at": "08:15"}, headers=mine
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # A meal reminder without a slot takes one from its time
    assert (body["slot"], body["at"], body["weekdays"]) == (
        "breakfast",
        "08:15:00",
        [0, 1, 2, 3, 4, 5, 6],
    )
    path = f"{PREFIX}/reminders/{body['id']}"

    moved = await client.patch(
        path, json={"at": "09:00", "weekdays": [5, 6], "enabled": False}, headers=mine
    )
    assert (moved.json()["at"], moved.json()["weekdays"], moved.json()["enabled"]) == (
        "09:00:00",
        [5, 6],
        False,
    )

    assert (await client.get(f"{PREFIX}/reminders", headers=theirs)).json() == []
    assert (await client.patch(path, json={"enabled": True}, headers=theirs)).status_code == 404
    assert (await client.delete(path, headers=theirs)).status_code == 404
    assert (await client.delete(path, headers=mine)).status_code == 204


async def test_a_reminder_needs_at_least_one_real_day(client: AsyncClient):
    headers = await auth_headers(client, "days@example.com")

    for weekdays in ([], [7], [-1]):
        response = await client.post(
            f"{PREFIX}/reminders",
            json={"kind": "water", "at": "10:00", "weekdays": weekdays},
            headers=headers,
        )
        assert response.status_code == 422, weekdays


async def test_settings_take_a_real_timezone_only(client: AsyncClient):
    headers = await auth_headers(client, "tz@example.com")

    good = await client.patch(
        f"{PREFIX}/eating/settings", json={"timezone": "Europe/Belgrade"}, headers=headers
    )
    bad = await client.patch(
        f"{PREFIX}/eating/settings", json={"timezone": "Mars/Olympus"}, headers=headers
    )

    assert good.json()["timezone"] == "Europe/Belgrade"
    assert bad.status_code == 422


# --- Browsers -----------------------------------------------------------------


async def test_push_is_off_until_the_server_has_keys(client: AsyncClient, monkeypatch):
    headers = await auth_headers(client, "config@example.com")
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", None)

    off = await client.get(f"{PREFIX}/push/config", headers=headers)
    assert off.json() == {"enabled": False, "public_key": None}
    assert (await client.post(f"{PREFIX}/push/test", headers=headers)).status_code == 503

    public, private = push.new_vapid_keys()
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", public)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", private)
    on = await client.get(f"{PREFIX}/push/config", headers=headers)
    assert on.json() == {"enabled": True, "public_key": public}


def test_a_vapid_pair_is_a_raw_p256_key():
    public, private = push.new_vapid_keys()

    def raw(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    assert len(raw(public)) == 65 and raw(public)[0] == 4
    assert len(raw(private)) == 32


async def test_only_real_push_services_are_accepted(client: AsyncClient):
    headers = await auth_headers(client, "ssrf@example.com")

    for endpoint in (
        "http://fcm.googleapis.com/fcm/send/x",
        "https://169.254.169.254/latest/meta-data",
        "https://fcm.googleapis.com.evil.example/x",
        "https://localhost:8004/api/v1/auth/me",
    ):
        response = await client.post(
            f"{PREFIX}/push/subscriptions",
            json={"endpoint": endpoint, "keys": KEYS},
            headers=headers,
        )
        assert response.status_code == 422, endpoint

    for endpoint in (
        FCM,
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/QGuQyavXutnMH",
    ):
        response = await client.post(
            f"{PREFIX}/push/subscriptions",
            json={"endpoint": endpoint, "keys": KEYS},
            headers=headers,
        )
        assert response.status_code == 201, endpoint


async def test_a_browser_belongs_to_whoever_subscribed_it_last(
    client: AsyncClient, session: AsyncSession
):
    first = await auth_headers(client, "first-phone@example.com")
    second = await auth_headers(client, "second-phone@example.com")
    body = {"endpoint": FCM, "keys": KEYS}

    await client.post(f"{PREFIX}/push/subscriptions", json=body, headers=first)
    await client.post(f"{PREFIX}/push/subscriptions", json=body, headers=second)

    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 1
    # The first person's unsubscribe cannot drop the second person's browser
    await client.request(
        "DELETE", f"{PREFIX}/push/subscriptions", json={"endpoint": FCM}, headers=first
    )
    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 1
    await client.request(
        "DELETE", f"{PREFIX}/push/subscriptions", json={"endpoint": FCM}, headers=second
    )
    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 0


# --- The loop -------------------------------------------------------------------


@pytest.fixture
def outbox(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []

    def sender(endpoint: str, p256dh: str, auth: str, message: dict) -> push.Outcome:
        sent.append((endpoint, message))
        return "gone" if "gone" in endpoint else "sent"

    monkeypatch.setattr(push, "send", sender)
    return sent


async def _subscribed(client: AsyncClient, email: str, endpoint: str = FCM) -> dict:
    headers = await auth_headers(client, email)
    await client.patch(
        f"{PREFIX}/eating/settings",
        json={"timezone": "Europe/Belgrade", "target_kcal": 2000},
        headers=headers,
    )
    await client.post(
        f"{PREFIX}/push/subscriptions", json={"endpoint": endpoint, "keys": KEYS}, headers=headers
    )
    return headers


LUNCHTIME = datetime(2026, 9, 21, 11, 2, tzinfo=UTC)  # 13:02 in Belgrade


async def test_a_due_reminder_goes_out_once(client: AsyncClient, outbox):
    headers = await _subscribed(client, "loop@example.com")
    await client.post(
        f"{PREFIX}/reminders",
        json={"kind": "meal", "slot": "lunch", "at": "13:00"},
        headers=headers,
    )

    assert await reminders.tick(LUNCHTIME, push.send) == 1
    assert await reminders.tick(LUNCHTIME, push.send) == 0

    endpoint, message = outbox[0]
    assert endpoint == FCM
    assert message["title"] == "Lunch time"
    assert message["url"] == "/?add=lunch"
    listed = (await client.get(f"{PREFIX}/reminders", headers=headers)).json()
    assert listed[0]["last_sent_on"] == DAY


async def test_a_meal_already_logged_is_not_reminded_about(client: AsyncClient, outbox):
    headers = await _subscribed(client, "logged@example.com")
    await client.post(
        f"{PREFIX}/reminders",
        json={"kind": "meal", "slot": "lunch", "at": "13:00"},
        headers=headers,
    )
    await _meal(client, headers, slot="lunch", items=[{"label": "Supa", "macros": {"kcal": 150}}])

    assert await reminders.tick(LUNCHTIME, push.send) == 0
    assert outbox == []


async def test_water_and_the_summary_read_the_day(client: AsyncClient, outbox):
    headers = await _subscribed(client, "summary@example.com")
    await client.post(f"{PREFIX}/reminders", json={"kind": "water", "at": "13:00"}, headers=headers)
    await client.post(
        f"{PREFIX}/reminders", json={"kind": "summary", "at": "13:00"}, headers=headers
    )
    await client.post(f"{PREFIX}/eating/water", json={"day": DAY, "ml": 750}, headers=headers)
    await _meal(client, headers, items=[{"label": "Burek", "macros": {"kcal": 1234}}])

    await reminders.tick(LUNCHTIME, push.send)

    messages = {message["tag"]: message for _, message in outbox}
    assert messages["reminder-water"]["body"] == "0.8 l of 2.0 l so far today."
    assert messages["reminder-summary"]["body"].startswith("1 234 of 2 000 kcal and 0.8 l")


async def test_a_browser_that_unsubscribed_is_forgotten(
    client: AsyncClient, outbox, session: AsyncSession
):
    headers = await _subscribed(client, "gone@example.com", "https://fcm.googleapis.com/gone")
    await client.post(
        f"{PREFIX}/reminders", json={"kind": "summary", "at": "13:00"}, headers=headers
    )

    await reminders.tick(LUNCHTIME, push.send)

    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 0
