"""The reminder loop with the clock stopped: who is reminded, on which of
their own days, how often, and what happens to the browsers that answer —
plus the "send a test" button and the loop surviving a bad tick."""

import asyncio
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import push as push_routes
from app.core.config import settings
from app.models import PushSubscription, Reminder, User
from app.services import push, reminders
from tests.helpers import PREFIX, auth_headers

FCM = "https://fcm.googleapis.com/fcm/send/"
KEYS = {"p256dh": "BBrowsersPublicKeyStandIn", "auth": "AuthSecretStandIn"}
# Monday 21 September 2026, 11:02 UTC — 13:02 in Belgrade
LUNCHTIME = datetime(2026, 9, 21, 11, 2, tzinfo=UTC)


class Outbox:
    """Stands in for the push services: records what went where, and answers
    per browser the way a real service would ("gone", "failed")."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.answers: dict[str, push.Outcome] = {}

    def __call__(self, endpoint: str, p256dh: str, auth: str, message: dict) -> push.Outcome:
        self.sent.append((endpoint, message))
        return self.answers.get(endpoint, "sent")

    @property
    def tags(self) -> list[str]:
        return [message["tag"] for _, message in self.sent]


@pytest.fixture
def outbox(monkeypatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(push, "send", box)
    return box


async def person(
    client: AsyncClient,
    email: str,
    *,
    timezone: str = "Europe/Belgrade",
    browsers: tuple[str, ...] = ("phone",),
    **settings_body,
) -> dict:
    headers = await auth_headers(client, email)
    await client.patch(
        f"{PREFIX}/eating/settings", json={"timezone": timezone, **settings_body}, headers=headers
    )
    for browser in browsers:
        response = await client.post(
            f"{PREFIX}/push/subscriptions",
            json={"endpoint": f"{FCM}{email}-{browser}", "keys": KEYS},
            headers=headers,
        )
        assert response.status_code == 201, response.text
    return headers


async def remind(client: AsyncClient, headers: dict, **body) -> dict:
    response = await client.post(f"{PREFIX}/reminders", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- Whose clock --------------------------------------------------------------


async def test_a_reminder_goes_out_on_the_owners_own_date_and_weekday(
    client: AsyncClient, outbox: Outbox
):
    # 20:30 UTC on Monday is already 08:30 on Tuesday in Auckland
    headers = await person(client, "auckland@example.com", timezone="Pacific/Auckland")
    tuesdays = await remind(
        client, headers, kind="meal", slot="breakfast", at="08:30", weekdays=[1]
    )
    mondays = await remind(client, headers, kind="water", at="08:30", weekdays=[0])

    sent = await reminders.tick(datetime(2026, 9, 21, 20, 30, tzinfo=UTC), push.send)

    assert sent == 1
    assert outbox.tags == ["reminder-meal-breakfast"]
    listed = {
        row["id"]: row for row in (await client.get(f"{PREFIX}/reminders", headers=headers)).json()
    }
    assert listed[tuesdays["id"]]["last_sent_on"] == "2026-09-22"
    assert listed[mondays["id"]]["last_sent_on"] is None


async def test_a_meal_logged_on_another_day_does_not_silence_todays_reminder(
    client: AsyncClient, outbox: Outbox
):
    headers = await person(client, "yesterday@example.com")
    await remind(client, headers, kind="meal", slot="lunch", at="13:00")
    await client.post(
        f"{PREFIX}/eating/meals",
        json={"day": "2026-09-20", "slot": "lunch", "items": []},
        headers=headers,
    )

    assert await reminders.tick(LUNCHTIME, push.send) == 1


async def test_a_person_without_a_timezone_is_reminded_by_utc(
    client: AsyncClient, outbox: Outbox, session: AsyncSession
):
    headers = await person(client, "no-zone@example.com")
    await session.execute(
        update(User).where(User.email == "no-zone@example.com").values(timezone=None)
    )
    await session.commit()
    await remind(client, headers, kind="water", at="11:00")

    assert await reminders.tick(LUNCHTIME, push.send) == 1


def test_an_unknown_timezone_falls_back_to_utc():
    assert str(reminders.zone_of("Mars/Olympus")) == "UTC"
    assert str(reminders.zone_of(None)) == "UTC"
    assert str(reminders.zone_of("Europe/Belgrade")) == "Europe/Belgrade"


# --- Who is reminded ------------------------------------------------------------


async def test_every_browser_a_person_said_yes_on_gets_it(client: AsyncClient, outbox: Outbox):
    headers = await person(client, "two-devices@example.com", browsers=("phone", "laptop"))
    await remind(client, headers, kind="summary", at="13:00")

    assert await reminders.tick(LUNCHTIME, push.send) == 2
    assert sorted(endpoint.rsplit("-", 1)[-1] for endpoint, _ in outbox.sent) == [
        "laptop",
        "phone",
    ]


async def test_nobody_else_is_sent_my_reminder(client: AsyncClient, outbox: Outbox):
    mine = await person(client, "mine@example.com")
    await person(client, "theirs@example.com")
    await remind(client, mine, kind="water", at="13:00")

    await reminders.tick(LUNCHTIME, push.send)

    assert [endpoint for endpoint, _ in outbox.sent] == [f"{FCM}mine@example.com-phone"]


async def test_a_reminder_with_no_browser_waits_rather_than_being_spent(
    client: AsyncClient, outbox: Outbox
):
    headers = await person(client, "later@example.com", browsers=())
    await remind(client, headers, kind="water", at="13:00")

    assert await reminders.tick(LUNCHTIME, push.send) == 0
    listed = (await client.get(f"{PREFIX}/reminders", headers=headers)).json()
    assert listed[0]["last_sent_on"] is None

    # Notifications switched on a few minutes later, still inside the grace
    await client.post(
        f"{PREFIX}/push/subscriptions",
        json={"endpoint": f"{FCM}later-phone", "keys": KEYS},
        headers=headers,
    )
    assert await reminders.tick(LUNCHTIME.replace(minute=10), push.send) == 1


async def test_a_deactivated_account_is_not_reminded(
    client: AsyncClient, outbox: Outbox, session: AsyncSession
):
    headers = await person(client, "gone-quiet@example.com")
    await remind(client, headers, kind="water", at="13:00")
    await session.execute(
        update(User).where(User.email == "gone-quiet@example.com").values(is_active=False)
    )
    await session.commit()

    assert await reminders.tick(LUNCHTIME, push.send) == 0
    assert outbox.sent == []


async def test_a_switched_off_reminder_stays_quiet(client: AsyncClient, outbox: Outbox):
    headers = await person(client, "off@example.com")
    await remind(client, headers, kind="water", at="13:00", enabled=False)

    assert await reminders.tick(LUNCHTIME, push.send) == 0


# --- What the browsers answer ---------------------------------------------------


async def test_a_failing_push_service_keeps_the_browser_and_a_gone_one_drops_it(
    client: AsyncClient, outbox: Outbox, session: AsyncSession
):
    headers = await person(client, "flaky@example.com", browsers=("phone", "laptop"))
    outbox.answers[f"{FCM}flaky@example.com-phone"] = "failed"
    outbox.answers[f"{FCM}flaky@example.com-laptop"] = "gone"
    await remind(client, headers, kind="summary", at="13:00")

    assert await reminders.tick(LUNCHTIME, push.send) == 0

    left = list((await session.execute(select(PushSubscription.endpoint))).scalars())
    assert left == [f"{FCM}flaky@example.com-phone"]


# --- Once a day, and again after a change -----------------------------------------


@pytest.fixture
def lunchtime(monkeypatch) -> datetime:
    """The API's own clock stopped at the loop's, for edits made "now"."""
    monkeypatch.setattr(push_routes, "_now", lambda: LUNCHTIME)
    return LUNCHTIME


async def test_moving_a_reminder_lets_it_go_out_again_the_same_day(
    client: AsyncClient, outbox: Outbox, lunchtime
):
    headers = await person(client, "moved@example.com")
    reminder = await remind(client, headers, kind="water", at="13:00")
    assert await reminders.tick(LUNCHTIME, push.send) == 1

    moved = await client.patch(
        f"{PREFIX}/reminders/{reminder['id']}", json={"at": "16:00"}, headers=headers
    )

    assert moved.json()["last_sent_on"] is None
    assert await reminders.tick(datetime(2026, 9, 21, 14, 1, tzinfo=UTC), push.send) == 1
    assert await reminders.tick(datetime(2026, 9, 21, 14, 2, tzinfo=UTC), push.send) == 0


@pytest.mark.parametrize("earlier", ["12:45", "13:00"])
async def test_moving_a_sent_reminder_to_a_time_already_past_does_not_send_it_again(
    client: AsyncClient, outbox: Outbox, lunchtime, earlier
):
    headers = await person(client, "earlier@example.com")
    reminder = await remind(client, headers, kind="water", at="13:00")
    assert await reminders.tick(LUNCHTIME, push.send) == 1

    moved = await client.patch(
        f"{PREFIX}/reminders/{reminder['id']}", json={"at": earlier}, headers=headers
    )

    assert moved.json()["last_sent_on"] == "2026-09-21"
    assert await reminders.tick(datetime(2026, 9, 21, 11, 3, tzinfo=UTC), push.send) == 0
    # Tomorrow it goes out at its new time
    assert await reminders.tick(datetime(2026, 9, 22, 11, 1, tzinfo=UTC), push.send) == 1


async def test_switching_a_reminder_off_and_on_does_not_send_it_twice(
    client: AsyncClient, outbox: Outbox
):
    headers = await person(client, "toggle@example.com")
    reminder = await remind(client, headers, kind="water", at="13:00")
    path = f"{PREFIX}/reminders/{reminder['id']}"
    await reminders.tick(LUNCHTIME, push.send)

    await client.patch(path, json={"enabled": False}, headers=headers)
    await client.patch(path, json={"enabled": True}, headers=headers)

    assert await reminders.tick(LUNCHTIME.replace(minute=5), push.send) == 0


async def test_it_goes_out_again_the_next_day(client: AsyncClient, outbox: Outbox):
    headers = await person(client, "daily@example.com")
    await remind(client, headers, kind="water", at="13:00")

    assert await reminders.tick(LUNCHTIME, push.send) == 1
    assert await reminders.tick(datetime(2026, 9, 22, 11, 2, tzinfo=UTC), push.send) == 1


# --- What the message says --------------------------------------------------------


async def test_the_summary_of_an_empty_day_invites_rather_than_scolds(
    client: AsyncClient, outbox: Outbox
):
    headers = await person(client, "empty-day@example.com")
    await remind(client, headers, kind="summary", at="13:00")

    await reminders.tick(LUNCHTIME, push.send)

    assert outbox.sent[0][1]["body"] == "Nothing logged today yet — anything you ate still counts."


async def test_the_summary_leaves_the_target_out_when_there_is_none(
    client: AsyncClient, outbox: Outbox
):
    headers = await person(client, "no-target@example.com")
    await remind(client, headers, kind="summary", at="13:00")
    await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": "2026-09-21",
            "items": [{"label": "Pljeskavica", "macros": {"kcal": 850}}],
        },
        headers=headers,
    )

    await reminders.tick(LUNCHTIME, push.send)

    assert outbox.sent[0][1]["body"] == "850 kcal and 0.0 l of water. Anything left to log?"


async def test_water_stays_quiet_once_the_goal_is_drunk(client: AsyncClient, outbox: Outbox):
    headers = await person(client, "hydrated@example.com", water_goal_ml=1500)
    await remind(client, headers, kind="water", at="13:00")
    for _ in range(3):
        await client.post(
            f"{PREFIX}/eating/water", json={"day": "2026-09-21", "ml": 500}, headers=headers
        )

    assert await reminders.tick(LUNCHTIME, push.send) == 0


# --- Asking for reminders -----------------------------------------------------------


async def test_twenty_reminders_is_the_most(client: AsyncClient):
    headers = await auth_headers(client, "many@example.com")
    for minute in range(20):
        await remind(client, headers, kind="water", at=f"10:{minute:02d}")

    response = await client.post(
        f"{PREFIX}/reminders", json={"kind": "water", "at": "11:00"}, headers=headers
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "20 reminders is the most — remove one first"
    # Someone else's twenty do not count against mine
    other = await auth_headers(client, "few@example.com")
    assert (
        await client.post(
            f"{PREFIX}/reminders", json={"kind": "water", "at": "11:00"}, headers=other
        )
    ).status_code == 201


async def test_only_a_meal_reminder_has_a_slot(client: AsyncClient):
    headers = await auth_headers(client, "slots@example.com")

    water = await remind(client, headers, kind="water", at="08:00", slot="breakfast")
    evening = await remind(client, headers, kind="meal", at="22:15")
    moved = await client.patch(
        f"{PREFIX}/reminders/{water['id']}", json={"slot": "dinner"}, headers=headers
    )

    assert water["slot"] is None and moved.json()["slot"] is None
    assert evening["slot"] == "snack"
    # Seconds are dropped: a reminder is set to the minute
    assert (await remind(client, headers, kind="water", at="09:30:45"))["at"] == "09:30:00"


async def test_a_reminder_kind_or_time_that_does_not_exist_is_refused(client: AsyncClient):
    headers = await auth_headers(client, "bad-reminder@example.com")

    for body in (
        {"kind": "coffee", "at": "08:00"},
        {"kind": "water", "at": "25:00"},
        {"kind": "meal", "slot": "brunch", "at": "11:00"},
        {"kind": "water"},
    ):
        response = await client.post(f"{PREFIX}/reminders", json=body, headers=headers)
        assert response.status_code == 422, body


async def test_reminders_and_browsers_need_a_session(client: AsyncClient):
    for method, path in (
        ("GET", "/reminders"),
        ("POST", "/reminders"),
        ("GET", "/push/config"),
        ("POST", "/push/subscriptions"),
        ("POST", "/push/test"),
    ):
        response = await client.request(method, f"{PREFIX}{path}", json={})
        assert response.status_code == 401, path


# --- "Send a test" --------------------------------------------------------------------


@pytest.fixture
def push_on(monkeypatch) -> None:
    public, private = push.new_vapid_keys()
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", public)
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", private)


async def test_a_test_notification_goes_to_each_browser_and_counts_what_arrived(
    client: AsyncClient, outbox: Outbox, push_on, session: AsyncSession
):
    headers = await person(client, "tester@example.com", browsers=("phone", "laptop", "old"))
    outbox.answers[f"{FCM}tester@example.com-laptop"] = "failed"
    outbox.answers[f"{FCM}tester@example.com-old"] = "gone"

    response = await client.post(f"{PREFIX}/push/test", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"sent": 1, "failed": 2}
    assert outbox.sent[0][1]["title"] == "Reminders are on"
    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 2


async def test_a_test_notification_needs_a_browser_first(
    client: AsyncClient, outbox: Outbox, push_on
):
    headers = await auth_headers(client, "no-browser@example.com")

    response = await client.post(f"{PREFIX}/push/test", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Turn notifications on first"


async def test_unsubscribing_a_browser_twice_or_one_never_seen_is_harmless(client: AsyncClient):
    headers = await person(client, "twice@example.com")
    body = {"endpoint": f"{FCM}twice@example.com-phone"}

    for _ in range(2):
        response = await client.request(
            "DELETE", f"{PREFIX}/push/subscriptions", json=body, headers=headers
        )
        assert response.status_code == 204


# --- The loop itself ---------------------------------------------------------------------


async def test_the_loop_survives_a_bad_tick_and_skips_sending_while_push_is_off(monkeypatch):
    calls: list[str] = []
    stop = asyncio.Event()

    async def tick(now):
        calls.append("tick")

    async def tidy(now):
        calls.append("tidy")
        if calls.count("tidy") == 1:
            raise RuntimeError("database blinked")
        stop.set()

    monkeypatch.setattr(reminders, "tick", tick)
    monkeypatch.setattr(reminders, "tidy", tidy)
    monkeypatch.setattr(settings, "REMINDER_TICK_SECONDS", 0)

    await asyncio.wait_for(reminders.run(stop), timeout=5)

    # Push is off in the suite: only the housekeeping ran, and it ran again
    # after the first round failed
    assert calls == ["tidy", "tidy"]


async def test_the_loop_sends_when_the_server_has_keys(monkeypatch, push_on):
    calls: list[str] = []
    stop = asyncio.Event()

    async def tick(now):
        calls.append("tick")

    async def tidy(now):
        calls.append("tidy")
        stop.set()

    monkeypatch.setattr(reminders, "tick", tick)
    monkeypatch.setattr(reminders, "tidy", tidy)

    await asyncio.wait_for(reminders.run(stop), timeout=5)

    assert calls == ["tick", "tidy"]


async def test_reminders_go_with_the_account(client: AsyncClient, session: AsyncSession):
    headers = await person(client, "leaving-reminders@example.com")
    await remind(client, headers, kind="water", at="13:00")

    await client.delete(f"{PREFIX}/auth/me", headers=headers)

    assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
    assert await session.scalar(select(func.count()).select_from(PushSubscription)) == 0
