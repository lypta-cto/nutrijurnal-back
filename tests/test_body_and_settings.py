"""The edges of water, weight and the settings behind the targets: ranges,
bounds, the first-run flag, and how a weighing and the goal calculator's
answers live side by side on one account."""

from datetime import datetime

import pytest
from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"
PROFILE = {
    "sex": "female",
    "birth_year": 1990,
    "height_cm": 168,
    "weight_kg": 70,
    "activity": "light",
    "goal": "maintain",
    "pace": 0.5,
    "protein_per_kg": None,
    "fat_percent": 30,
}


@pytest.fixture
async def me(client: AsyncClient) -> dict:
    return await auth_headers(client, "body@example.com")


# --- Water ------------------------------------------------------------------------


async def test_a_day_without_water_reads_as_none_against_the_usual_goal(client: AsyncClient, me):
    day = (await client.get(f"{PREFIX}/eating/water/{DAY}", headers=me)).json()

    assert day == {"day": DAY, "ml": 0, "goal_ml": 2000, "glass_ml": 250, "entries": []}


async def test_a_glass_that_is_not_there_can_not_be_taken_back(client: AsyncClient, me):
    missing = "00000000-0000-0000-0000-000000000000"

    assert (await client.delete(f"{PREFIX}/eating/water/{missing}", headers=me)).status_code == 404
    assert (await client.delete(f"{PREFIX}/eating/water/not-an-id", headers=me)).status_code == 422


async def test_a_glass_counts_on_its_own_day_only(client: AsyncClient, me):
    await client.post(f"{PREFIX}/eating/water", json={"day": DAY, "ml": 500}, headers=me)
    await client.post(f"{PREFIX}/eating/water", json={"day": "2026-09-22", "ml": 330}, headers=me)

    today = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    tomorrow = (await client.get(f"{PREFIX}/eating/days/2026-09-22", headers=me)).json()

    assert (today["water_ml"], tomorrow["water_ml"]) == (500, 330)


async def test_the_largest_glass_is_five_litres(client: AsyncClient, me):
    jug = await client.post(f"{PREFIX}/eating/water", json={"day": DAY, "ml": 5000}, headers=me)

    assert jug.status_code == 201
    assert jug.json()["ml"] == 5000


@pytest.mark.parametrize("path", ["water", "weight"])
async def test_a_range_must_run_forwards_and_stay_under_400_days(client: AsyncClient, me, path):
    async def listing(start: str, end: str) -> int:
        response = await client.get(
            f"{PREFIX}/eating/{path}", params={"from": start, "to": end}, headers=me
        )
        return response.status_code

    assert await listing("2026-09-21", "2026-09-20") == 400
    assert await listing("2025-08-16", "2026-09-21") == 400
    assert await listing("2025-08-17", "2026-09-21") == 200
    assert await listing(DAY, DAY) == 200


# --- Weight -------------------------------------------------------------------------


@pytest.mark.parametrize(("kg", "status"), [(20, 200), (400, 200), (19.9, 422), (400.1, 422)])
async def test_a_weight_is_between_twenty_and_four_hundred_kilos(
    client: AsyncClient, me, kg, status
):
    response = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": kg}, headers=me)

    assert response.status_code == status


async def test_nobody_weighed_yet_has_no_latest_weight(client: AsyncClient, me):
    assert (await client.get(f"{PREFIX}/eating/weight/latest", headers=me)).json() is None


async def test_a_weighing_rounds_into_the_calculator_to_one_decimal(client: AsyncClient, me):
    await client.patch(f"{PREFIX}/eating/settings", json={"profile": PROFILE}, headers=me)

    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 68.46}, headers=me)

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["profile"]["weight_kg"] == 68.5


@pytest.mark.parametrize("kg", [380, 25])
async def test_a_weighing_outside_the_calculators_range_does_not_break_the_settings(
    client: AsyncClient, me, kg
):
    """The scale takes 20–400 kg, the calculator 30–350: such a weighing is
    kept as a weighing, and the calculator keeps the weight it had."""
    await client.patch(f"{PREFIX}/eating/settings", json={"profile": PROFILE}, headers=me)
    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": kg}, headers=me)

    response = await client.get(f"{PREFIX}/eating/settings", headers=me)

    assert response.status_code == 200
    assert response.json()["profile"]["weight_kg"] == 70
    assert (await client.get(f"{PREFIX}/auth/me/export", headers=me)).status_code == 200
    latest = await client.get(f"{PREFIX}/eating/weight/latest", headers=me)
    assert latest.json()["kg"] == kg


async def test_deleting_the_newest_weighing_hands_the_calculator_the_one_before(
    client: AsyncClient, me
):
    await client.patch(f"{PREFIX}/eating/settings", json={"profile": PROFILE}, headers=me)
    await client.put(f"{PREFIX}/eating/weight/2026-09-20", json={"kg": 69.8}, headers=me)
    # 96.8 typed for 69.8, and taken back
    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 96.8}, headers=me)

    await client.delete(f"{PREFIX}/eating/weight/{DAY}", headers=me)

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["profile"]["weight_kg"] == 69.8


async def test_deleting_an_older_weighing_leaves_the_calculator_alone(client: AsyncClient, me):
    await client.patch(f"{PREFIX}/eating/settings", json={"profile": PROFILE}, headers=me)
    await client.put(f"{PREFIX}/eating/weight/2026-09-20", json={"kg": 71.2}, headers=me)
    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 70.4}, headers=me)

    await client.delete(f"{PREFIX}/eating/weight/2026-09-20", headers=me)

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()
    assert settings["profile"]["weight_kg"] == 70.4


# --- Settings and the first run -------------------------------------------------------


async def test_a_new_account_starts_unmeasured_and_not_onboarded(client: AsyncClient, me):
    settings = (await client.get(f"{PREFIX}/eating/settings", headers=me)).json()

    assert settings["onboarded_at"] is None
    assert settings["profile"] is None
    assert [settings[f"target_{name}"] for name in ("kcal", "protein", "carbs", "fat")] == [
        None,
        None,
        None,
        None,
    ]
    assert (settings["water_goal_ml"], settings["water_glass_ml"]) == (2000, 250)
    me_body = (await client.get(f"{PREFIX}/auth/me", headers=me)).json()
    assert me_body["onboarded_at"] is None


async def test_onboarding_is_marked_once_and_never_moves(client: AsyncClient, me):
    first = await client.patch(f"{PREFIX}/eating/settings", json={"onboarded": True}, headers=me)
    again = await client.patch(
        f"{PREFIX}/eating/settings", json={"onboarded": True, "target_kcal": 1900}, headers=me
    )
    unset = await client.patch(f"{PREFIX}/eating/settings", json={"onboarded": False}, headers=me)

    def moment(body) -> datetime:
        # SQLite hands timestamps back without their zone; the moment is what counts
        return datetime.fromisoformat(body.json()["onboarded_at"]).replace(tzinfo=None)

    stamped = moment(first)
    assert moment(again) == stamped
    assert moment(unset) == stamped
    # The page's guard reads it off the signed-in user
    assert (await client.get(f"{PREFIX}/auth/me", headers=me)).json()["onboarded_at"] is not None


async def test_skipping_the_calculator_still_counts_as_onboarded(client: AsyncClient, me):
    response = await client.patch(f"{PREFIX}/eating/settings", json={"onboarded": True}, headers=me)

    assert response.json()["onboarded_at"] is not None
    assert response.json()["target_kcal"] is None


async def test_a_target_can_be_cleared_without_touching_the_others(client: AsyncClient, me):
    await client.patch(
        f"{PREFIX}/eating/settings",
        json={"target_kcal": 2000, "target_protein": 150, "target_carbs": 200, "target_fat": 67},
        headers=me,
    )

    cleared = await client.patch(
        f"{PREFIX}/eating/settings", json={"target_carbs": None}, headers=me
    )

    body = cleared.json()
    assert (body["target_kcal"], body["target_protein"], body["target_carbs"]) == (2000, 150, None)
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()
    assert day["target"]["kcal"] == 2000


@pytest.mark.parametrize(
    "body",
    [
        {"water_goal_ml": 249},
        {"water_goal_ml": 10001},
        {"water_glass_ml": 49},
        {"water_glass_ml": 2001},
        {"timezone": "Europe/Atlantis"},
        {"profile": {**PROFILE, "fat_percent": 50}},
        {"profile": {**PROFILE, "activity": "couch"}},
        {"profile": {**PROFILE, "protein_per_kg": 4}},
    ],
)
async def test_settings_out_of_bounds_are_refused(client: AsyncClient, me, body):
    response = await client.patch(f"{PREFIX}/eating/settings", json=body, headers=me)

    assert response.status_code == 422


async def test_the_answers_saved_feed_the_estimate_they_came_from(client: AsyncClient, me):
    saved = await client.patch(f"{PREFIX}/eating/settings", json={"profile": PROFILE}, headers=me)

    estimate = await client.post(
        f"{PREFIX}/eating/goals/estimate", json=saved.json()["profile"], headers=me
    )

    assert estimate.status_code == 200
    assert estimate.json()["kcal"] >= 1200


async def test_maintaining_ignores_the_pace(client: AsyncClient, me):
    fast = await client.post(
        f"{PREFIX}/eating/goals/estimate", json={**PROFILE, "pace": 1}, headers=me
    )
    still = await client.post(
        f"{PREFIX}/eating/goals/estimate", json={**PROFILE, "pace": 0}, headers=me
    )

    assert fast.json()["kcal"] == still.json()["kcal"]
    assert fast.json()["daily_change"] == still.json()["daily_change"]
