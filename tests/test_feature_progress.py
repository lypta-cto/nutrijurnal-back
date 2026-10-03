"""Progress: a period in one answer — each day's food, water and weight, the
streak of days written down and what a written-down day averaged."""

from datetime import date, timedelta

from httpx import AsyncClient

from app.services.progress import current_streak, longest_streak
from tests.helpers import PREFIX, auth_headers

D = date(2026, 9, 21)


def days(*offsets: int) -> set[date]:
    return {D + timedelta(days=offset) for offset in offsets}


def test_a_streak_runs_back_from_today():
    assert current_streak(days(-2, -1, 0), D) == 3


def test_an_empty_today_does_not_break_the_streak_yet():
    assert current_streak(days(-3, -2, -1), D) == 3


def test_a_missed_day_ends_the_streak():
    assert current_streak(days(-5, -4, -3, -1, 0), D) == 2
    assert current_streak(days(-5, -4), D) == 0


def test_the_longest_run_is_found_anywhere():
    assert longest_streak(days(-20, -19, -18, -17, -10, -9, 0)) == 4
    assert longest_streak(set()) == 0


async def _quick(client: AsyncClient, headers: dict, day: str, kcal: float, protein: float = 0):
    response = await client.post(
        f"{PREFIX}/eating/meals",
        json={
            "day": day,
            "items": [{"label": "Plate", "macros": {"kcal": kcal, "protein": protein}}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


async def test_a_period_in_one_answer(client: AsyncClient):
    headers = await auth_headers(client, "progress@example.com")
    await client.patch(
        f"{PREFIX}/eating/settings",
        json={"target_kcal": 2000, "water_goal_ml": 2500},
        headers=headers,
    )
    await _quick(client, headers, "2026-09-19", 1800, 100)
    await _quick(client, headers, "2026-09-20", 1500, 80)
    await _quick(client, headers, "2026-09-20", 700, 40)
    await _quick(client, headers, "2026-09-21", 2100, 120)
    await client.post(
        f"{PREFIX}/eating/water", json={"day": "2026-09-20", "ml": 1500}, headers=headers
    )
    await client.post(
        f"{PREFIX}/eating/water", json={"day": "2026-09-21", "ml": 2500}, headers=headers
    )
    await client.put(f"{PREFIX}/eating/weight/2026-09-18", json={"kg": 80.4}, headers=headers)
    await client.put(f"{PREFIX}/eating/weight/2026-09-21", json={"kg": 79.8}, headers=headers)

    response = await client.get(
        f"{PREFIX}/eating/progress",
        params={"from": "2026-09-18", "to": "2026-09-21", "today": "2026-09-21"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(entry["day"], entry["meals"], entry["kcal"]) for entry in body["days"]] == [
        ("2026-09-18", 0, 0),
        ("2026-09-19", 1, 1800),
        ("2026-09-20", 2, 2200),
        ("2026-09-21", 1, 2100),
    ]
    assert [entry["water_ml"] for entry in body["days"]] == [0, 0, 1500, 2500]
    assert [entry["weight_kg"] for entry in body["days"]] == [80.4, None, None, 79.8]
    assert (body["target_kcal"], body["water_goal_ml"]) == (2000, 2500)
    # Only the days with meals are averaged — an empty day is unknown, not zero
    averages = body["averages"]
    assert (averages["kcal"], averages["protein"]) == (2033.3, 113.3)
    assert (averages["logged_days"], averages["days"]) == (3, 4)
    assert averages["water_ml"] == 2000
    assert body["streak"] == {"current": 3, "longest": 3, "logged_today": True}
    assert body["weight"]["change"] == -0.6


async def test_the_streak_counts_the_whole_diary_not_only_the_period(client: AsyncClient):
    headers = await auth_headers(client, "streak@example.com")
    for day in ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"):
        await _quick(client, headers, day, 500)

    response = await client.get(
        f"{PREFIX}/eating/progress",
        params={"from": "2026-09-19", "to": "2026-09-20", "today": "2026-09-20"},
        headers=headers,
    )

    assert response.json()["streak"] == {"current": 5, "longest": 5, "logged_today": False}


async def test_progress_is_ones_own(client: AsyncClient):
    owner = await auth_headers(client, "progress-owner@example.com")
    stranger = await auth_headers(client, "progress-stranger@example.com")
    await _quick(client, owner, "2026-09-21", 2000)
    await client.put(f"{PREFIX}/eating/weight/2026-09-21", json={"kg": 70}, headers=owner)

    response = await client.get(
        f"{PREFIX}/eating/progress",
        params={"from": "2026-09-21", "to": "2026-09-21"},
        headers=stranger,
    )

    body = response.json()
    assert body["days"][0]["meals"] == 0 and body["days"][0]["weight_kg"] is None
    assert body["streak"]["longest"] == 0
    assert body["averages"]["kcal"] is None


async def test_a_backwards_or_endless_range_is_refused(client: AsyncClient):
    headers = await auth_headers(client, "range-progress@example.com")

    backwards = await client.get(
        f"{PREFIX}/eating/progress",
        params={"from": "2026-09-21", "to": "2026-09-01"},
        headers=headers,
    )
    endless = await client.get(
        f"{PREFIX}/eating/progress",
        params={"from": "2024-01-01", "to": "2026-09-01"},
        headers=headers,
    )

    assert backwards.status_code == 400 and endless.status_code == 400
