"""
From a body and a goal to a daily target.

Mifflin–St Jeor for the energy burned at rest, a standard activity factor for
an ordinary day, and a deficit or surplus for the goal at roughly 7 700 kcal
per kilogram. The macros follow from it: protein by bodyweight, fat as a share
of the energy, carbohydrate whatever is left. Every number is a starting point
the person can overwrite — this is an estimate, not a prescription.
"""

from __future__ import annotations

from datetime import date

from app.schemas.goals import MAX_WEIGHT_KG, MIN_WEIGHT_KG, GoalEstimate, GoalProfile

ACTIVITY_FACTORS: dict[str, float] = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

# Mifflin–St Jeor's constant per sex; someone who would rather not say gets
# the midpoint, which lands within a few percent of either
SEX_OFFSET: dict[str, float] = {"male": 5, "female": -161, "other": -78}

# The usual lower bounds for an unsupervised diet. A goal that asks for less
# is raised to it, and the estimate says so.
KCAL_FLOOR: dict[str, int] = {"male": 1500, "female": 1200, "other": 1350}

KCAL_PER_KG = 7700

# Gaining faster than half a kilo a week is mostly gaining fat
MAX_PACE: dict[str, float] = {"lose": 1.0, "maintain": 0.0, "gain": 0.5}

# Higher while losing, to keep the muscle; the ordinary amount otherwise
PROTEIN_PER_KG: dict[str, float] = {"lose": 1.8, "maintain": 1.6, "gain": 1.8}

# Protein by total bodyweight overshoots for a heavier body — past this share
# of the day's energy it starts crowding out everything else
MAX_PROTEIN_SHARE = 0.4


def age_on(birth_year: int, today: date) -> int:
    """Whole years, counted the way a form counts them: by the year alone."""
    return max(0, today.year - birth_year)


def resting_energy(profile: GoalProfile, age: int) -> float:
    return 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * age + SEX_OFFSET[profile.sex]


def estimate(profile: GoalProfile, today: date | None = None) -> GoalEstimate:
    age = age_on(profile.birth_year, today or date.today())
    bmr = resting_energy(profile, age)
    maintenance = bmr * ACTIVITY_FACTORS[profile.activity]

    pace = min(profile.pace, MAX_PACE[profile.goal])
    change = pace * KCAL_PER_KG / 7
    if profile.goal == "lose":
        change = -change
    elif profile.goal == "maintain":
        change = 0

    kcal = round((maintenance + change) / 10) * 10
    floor = KCAL_FLOOR[profile.sex]
    floored = kcal < floor
    kcal = max(kcal, floor)

    per_kg = profile.protein_per_kg or PROTEIN_PER_KG[profile.goal]
    protein = min(per_kg * profile.weight_kg, kcal * MAX_PROTEIN_SHARE / 4)
    fat = kcal * profile.fat_percent / 100 / 9
    carbs = max(0.0, (kcal - protein * 4 - fat * 9) / 4)

    return GoalEstimate(
        age=age,
        bmr=round(bmr),
        maintenance=round(maintenance),
        kcal=kcal,
        protein=round(protein),
        carbs=round(carbs),
        fat=round(fat),
        daily_change=round(kcal - maintenance),
        protein_per_kg=round(per_kg, 2),
        fat_percent=profile.fat_percent,
        floored=floored,
    )


def fits_calculator(kg: float) -> bool:
    return MIN_WEIGHT_KG <= kg <= MAX_WEIGHT_KG


def profile_of(user) -> GoalProfile | None:
    """The answers kept on the account, or None while they were never given.
    The weight is held inside the calculator's range: reading the settings
    must never fail on a number the scale was allowed to store."""
    if not (user.sex and user.birth_year and user.height_cm and user.weight_kg):
        return None
    return GoalProfile(
        sex=user.sex,
        birth_year=user.birth_year,
        height_cm=user.height_cm,
        weight_kg=min(max(user.weight_kg, MIN_WEIGHT_KG), MAX_WEIGHT_KG),
        activity=user.activity or "light",
        goal=user.goal or "maintain",
        pace=user.goal_pace if user.goal_pace is not None else 0.5,
        protein_per_kg=user.protein_per_kg,
        fat_percent=user.fat_percent or 30,
    )


def keep_profile(user, profile: GoalProfile) -> None:
    user.sex = profile.sex
    user.birth_year = profile.birth_year
    user.height_cm = profile.height_cm
    user.weight_kg = profile.weight_kg
    user.activity = profile.activity
    user.goal = profile.goal
    user.goal_pace = profile.pace
    user.protein_per_kg = profile.protein_per_kg
    user.fat_percent = profile.fat_percent
