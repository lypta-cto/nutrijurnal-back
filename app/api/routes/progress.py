"""
Progress: a period of the diary in one answer — every day's totals with its
water and weight, the streak of days written down, and the averages — so
the charts on the page all read the same slice.
"""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import distinct, select

from app.api.deps import CurrentUser, SessionDep
from app.api.routes import body
from app.api.routes.eating import _meals_between
from app.models.eating import Meal
from app.schemas.body import (
    Averages,
    ProgressDay,
    ProgressRead,
    Streak,
    WeightChange,
    WeightRead,
)
from app.services import nutrition, progress

router = APIRouter(prefix="/eating", tags=["progress"])

MAX_RANGE_DAYS = 400


@router.get("/progress", response_model=ProgressRead)
async def read_progress(
    session: SessionDep,
    user: CurrentUser,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
    today: date | None = Query(
        default=None, description="The viewer's own today, for the streak; the server's otherwise"
    ),
) -> ProgressRead:
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from is after to")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="range is too long")

    days = {
        start + timedelta(days=offset): ProgressDay(day=start + timedelta(days=offset))
        for offset in range((end - start).days + 1)
    }
    for meal in await _meals_between(session, user, start, end):
        entry = days[meal.day]
        entry.meals += 1
        sums = nutrition.total([item.macros for item in meal.items])
        for name, value in sums.items():
            setattr(entry, name, round(getattr(entry, name) + value, 1))
    for day, ml in (await body.water_totals(session, user, start, end)).items():
        days[day].water_ml = ml
    weights = await body.weights_between(session, user, start, end)
    for entry in weights:
        days[entry.day].weight_kg = entry.kg

    logged_days = set(
        (await session.execute(select(distinct(Meal.day)).where(Meal.user_id == user.id))).scalars()
    )
    viewer_today = today or date.today()

    eaten = [entry for entry in days.values() if entry.meals]
    drunk = [entry.water_ml for entry in days.values() if entry.water_ml]
    first = WeightRead.model_validate(weights[0]) if weights else None
    last = WeightRead.model_validate(weights[-1]) if weights else None
    moved = round(last.kg - first.kg, 1) if first and last and first.day != last.day else None

    return ProgressRead(
        days=list(days.values()),
        target_kcal=user.target_kcal,
        target_protein=user.target_protein,
        target_carbs=user.target_carbs,
        target_fat=user.target_fat,
        water_goal_ml=body.water_goal(user),
        streak=Streak(
            current=progress.current_streak(logged_days, viewer_today),
            longest=progress.longest_streak(logged_days),
            logged_today=viewer_today in logged_days,
        ),
        averages=Averages(
            kcal=progress.average([entry.kcal for entry in eaten]),
            protein=progress.average([entry.protein for entry in eaten]),
            carbs=progress.average([entry.carbs for entry in eaten]),
            fat=progress.average([entry.fat for entry in eaten]),
            water_ml=progress.average(drunk),
            logged_days=len(eaten),
            days=len(days),
        ),
        weight=WeightChange(
            first=first,
            last=last,
            change=moved,
        ),
    )
