"""
Water and weight: the two numbers a food diary is read beside.

Both belong to one person only, like everything else in the diary — every
query filters on the signed-in user, and someone else's row is a 404.
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, SessionDep
from app.models.body import WaterEntry, WeightEntry
from app.schemas.body import WaterDay, WaterTotal, WaterWrite, WeightRead, WeightWrite
from app.schemas.eating import DiaryDay
from app.services import goals

router = APIRouter(prefix="/eating", tags=["water and weight"])

# Two litres in 250 ml glasses: the everyday rule of thumb, until a person
# sets their own
DEFAULT_WATER_GOAL_ML = 2000
DEFAULT_GLASS_ML = 250

MAX_RANGE_DAYS = 400


def water_goal(user) -> int:
    return user.water_goal_ml or DEFAULT_WATER_GOAL_ML


def glass_size(user) -> int:
    return user.water_glass_ml or DEFAULT_GLASS_ML


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from is after to")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="range is too long")


# --- Water ----------------------------------------------------------------------


async def water_totals(session, user, start: date, end: date) -> dict[date, int]:
    rows = await session.execute(
        select(WaterEntry.day, func.sum(WaterEntry.ml))
        .where(WaterEntry.user_id == user.id, WaterEntry.day >= start, WaterEntry.day <= end)
        .group_by(WaterEntry.day)
    )
    return {day: int(total or 0) for day, total in rows}


async def _water_day(session, user, day: date) -> WaterDay:
    entries = list(
        (
            await session.execute(
                select(WaterEntry)
                .where(WaterEntry.user_id == user.id, WaterEntry.day == day)
                .order_by(WaterEntry.created_at, WaterEntry.id)
            )
        ).scalars()
    )
    return WaterDay(
        day=day,
        ml=sum(entry.ml for entry in entries),
        goal_ml=water_goal(user),
        glass_ml=glass_size(user),
        entries=entries,
    )


@router.get("/water", response_model=list[WaterTotal])
async def list_water(
    session: SessionDep,
    user: CurrentUser,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
) -> list[WaterTotal]:
    _check_range(start, end)
    totals = await water_totals(session, user, start, end)
    return [WaterTotal(day=day, ml=ml) for day, ml in sorted(totals.items())]


@router.get("/water/{day}", response_model=WaterDay)
async def read_water(day: date, session: SessionDep, user: CurrentUser) -> WaterDay:
    return await _water_day(session, user, day)


@router.post("/water", response_model=WaterDay, status_code=status.HTTP_201_CREATED)
async def add_water(payload: WaterWrite, session: SessionDep, user: CurrentUser) -> WaterDay:
    # Stamped here rather than by the database clock: the glasses are listed
    # in the order they were drunk, and "now()" is the same for a whole
    # transaction (and only to the second on some databases)
    session.add(
        WaterEntry(user_id=user.id, day=payload.day, ml=payload.ml, created_at=datetime.now(UTC))
    )
    await session.flush()
    return await _water_day(session, user, payload.day)


@router.delete("/water/{entry_id}", response_model=WaterDay)
async def remove_water(entry_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> WaterDay:
    """One glass taken back — the undo of the "+" on the card."""
    entry = await session.get(WaterEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such entry")
    day = entry.day
    await session.delete(entry)
    await session.flush()
    return await _water_day(session, user, day)


# --- Weight ---------------------------------------------------------------------


async def weights_between(session, user, start: date, end: date) -> list[WeightEntry]:
    rows = await session.execute(
        select(WeightEntry)
        .where(WeightEntry.user_id == user.id, WeightEntry.day >= start, WeightEntry.day <= end)
        .order_by(WeightEntry.day)
    )
    return list(rows.scalars())


async def latest_weight(session, user) -> WeightEntry | None:
    return (
        await session.execute(
            select(WeightEntry)
            .where(WeightEntry.user_id == user.id)
            .order_by(WeightEntry.day.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/weight", response_model=list[WeightRead])
async def list_weight(
    session: SessionDep,
    user: CurrentUser,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
) -> list[WeightRead]:
    _check_range(start, end)
    entries = await weights_between(session, user, start, end)
    return [WeightRead.model_validate(entry) for entry in entries]


@router.get("/weight/latest", response_model=WeightRead | None)
async def read_latest_weight(session: SessionDep, user: CurrentUser) -> WeightRead | None:
    entry = await latest_weight(session, user)
    return WeightRead.model_validate(entry) if entry else None


@router.put("/weight/{day}", response_model=WeightRead)
async def write_weight(
    day: DiaryDay, payload: WeightWrite, session: SessionDep, user: CurrentUser
) -> WeightRead:
    """One number a day: weighing again the same day corrects it. The newest
    weight is also the one the goal calculator starts from — when it is one
    the calculator can take."""
    entry = (
        await session.execute(
            select(WeightEntry).where(WeightEntry.user_id == user.id, WeightEntry.day == day)
        )
    ).scalar_one_or_none()
    if entry is None:
        entry = WeightEntry(user_id=user.id, day=day, kg=payload.kg)
        session.add(entry)
    else:
        entry.kg = payload.kg
    await session.flush()
    newest = await latest_weight(session, user)
    if newest is not None and newest.day == day and goals.fits_calculator(payload.kg):
        user.weight_kg = round(payload.kg, 1)
        await session.flush()
    return WeightRead.model_validate(entry)


@router.delete("/weight/{day}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_weight(day: date, session: SessionDep, user: CurrentUser) -> None:
    entry = (
        await session.execute(
            select(WeightEntry).where(WeightEntry.user_id == user.id, WeightEntry.day == day)
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No weight that day")
    await session.delete(entry)
    await session.flush()
    # A mistyped newest weighing taken back must not stay in the calculator:
    # it starts from the one before (or keeps what was typed into it, if none)
    newest = await latest_weight(session, user)
    if newest is not None and newest.day < day and goals.fits_calculator(newest.kg):
        user.weight_kg = round(newest.kg, 1)
        await session.flush()
