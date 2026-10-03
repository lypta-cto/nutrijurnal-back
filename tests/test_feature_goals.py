"""The goal calculator: a body and a goal into a daily target, and the answers
kept on the account so the calculator reopens where it was left."""

from datetime import date

from httpx import AsyncClient

from app.schemas.goals import GoalProfile
from app.services import goals
from tests.helpers import PREFIX, auth_headers

TODAY = date(2026, 10, 3)


def test_a_maintenance_day_is_resting_energy_times_activity():
    profile = GoalProfile(
        sex="female", birth_year=1996, height_cm=165, weight_kg=60, activity="sedentary"
    )

    result = goals.estimate(profile, TODAY)

    # 10·60 + 6.25·165 − 5·30 − 161 = 1320.25, × 1.2 for a desk day
    assert result.age == 30
    assert result.bmr == 1320
    assert result.maintenance == 1584
    assert result.kcal == 1580
    assert result.daily_change == -4  # only the rounding to ten
    # Protein by bodyweight, fat as a share, carbohydrate whatever is left
    assert (result.protein, result.fat, result.carbs) == (96, 53, 180)
    assert result.floored is False


def test_losing_half_a_kilo_a_week_takes_550_kcal_off_and_raises_protein():
    profile = GoalProfile(
        sex="male",
        birth_year=1991,
        height_cm=180,
        weight_kg=80,
        activity="moderate",
        goal="lose",
        pace=0.5,
    )

    result = goals.estimate(profile, TODAY)

    assert result.bmr == 1755
    assert result.maintenance == 2720
    assert result.kcal == 2170
    assert result.daily_change == -550
    assert result.protein_per_kg == 1.8
    assert (result.protein, result.fat, result.carbs) == (144, 72, 236)


def test_a_goal_below_the_safe_minimum_is_raised_to_it_and_says_so():
    profile = GoalProfile(
        sex="female", birth_year=1966, height_cm=150, weight_kg=45, goal="lose", pace=1
    )

    result = goals.estimate(profile, TODAY)

    assert result.kcal == 1200
    assert result.floored is True


def test_gaining_is_capped_at_half_a_kilo_a_week():
    base = {"sex": "male", "birth_year": 2000, "height_cm": 175, "weight_kg": 70}
    steady = goals.estimate(GoalProfile(**base, activity="active"), TODAY)
    eager = goals.estimate(GoalProfile(**base, activity="active", goal="gain", pace=1), TODAY)

    assert eager.daily_change - steady.daily_change == 550


def test_protein_and_fat_can_be_set_by_hand():
    profile = GoalProfile(
        sex="other",
        birth_year=1990,
        height_cm=170,
        weight_kg=70,
        protein_per_kg=2.2,
        fat_percent=25,
    )

    result = goals.estimate(profile, TODAY)

    assert result.protein == 154
    assert result.fat_percent == 25
    assert result.fat == round(result.kcal * 0.25 / 9)


def test_a_heavy_body_does_not_get_a_protein_target_that_eats_the_day():
    profile = GoalProfile(
        sex="male", birth_year=1980, height_cm=180, weight_kg=180, goal="lose", pace=1
    )

    result = goals.estimate(profile, TODAY)

    assert result.protein * 4 <= result.kcal * 0.4 + 4
    assert result.carbs > 0


async def test_the_estimate_needs_a_session_and_saves_nothing(client: AsyncClient):
    body = {"sex": "female", "birth_year": 1996, "height_cm": 165, "weight_kg": 60}

    assert (await client.post(f"{PREFIX}/eating/goals/estimate", json=body)).status_code == 401

    headers = await auth_headers(client, "goals@example.com")
    response = await client.post(f"{PREFIX}/eating/goals/estimate", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["kcal"] > 1200

    settings = (await client.get(f"{PREFIX}/eating/settings", headers=headers)).json()
    assert settings["profile"] is None
    assert settings["target_kcal"] is None


async def test_too_young_or_impossible_bodies_are_turned_away(client: AsyncClient):
    headers = await auth_headers(client, "young@example.com")
    young = {"sex": "male", "birth_year": date.today().year - 10, "height_cm": 150}
    tiny = {"sex": "male", "birth_year": 1990, "height_cm": 40, "weight_kg": 70}

    for body in ({**young, "weight_kg": 40}, tiny):
        response = await client.post(f"{PREFIX}/eating/goals/estimate", json=body, headers=headers)
        assert response.status_code == 422


async def test_onboarding_keeps_the_answers_with_the_targets(client: AsyncClient):
    headers = await auth_headers(client, "keeps@example.com")
    profile = {
        "sex": "male",
        "birth_year": 1991,
        "height_cm": 180,
        "weight_kg": 80,
        "activity": "moderate",
        "goal": "lose",
        "pace": 0.5,
        "protein_per_kg": None,
        "fat_percent": 30,
    }

    response = await client.patch(
        f"{PREFIX}/eating/settings",
        json={
            "target_kcal": 2100,
            "target_protein": 150,
            "target_carbs": 220,
            "target_fat": 70,
            "profile": profile,
            "onboarded": True,
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"] == profile
    # The person's own numbers win over the estimate they started from
    assert body["target_kcal"] == 2100
    assert body["onboarded_at"] is not None

    # A later save without the profile leaves the answers alone
    again = await client.patch(
        f"{PREFIX}/eating/settings", json={"target_kcal": 2000}, headers=headers
    )
    assert again.json()["profile"] == profile

    # Nobody else sees them
    other = await auth_headers(client, "other@example.com")
    assert (await client.get(f"{PREFIX}/eating/settings", headers=other)).json()["profile"] is None
