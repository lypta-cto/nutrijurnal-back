"""
The reminder loop: once a minute, send what is due and tidy up.

A reminder is due when, in its owner's own timezone, today is one of its
days and the clock has just passed its time (within a grace window, so a
restart does not fire the morning's reminders at noon). Before anything is
sent the reminder is claimed with one conditional UPDATE — "set
last_sent_on = today where it is not today yet" — so however many workers
run this loop, each reminder goes out once a day.

The messages read the diary first: a meal reminder for a slot that is
already logged stays quiet, so does water once the goal is met; the daily
summary says how the day went.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, or_, select, update

from app.core import database
from app.core.config import settings
from app.models.body import WaterEntry
from app.models.eating import DeletedMeal, Meal, MealItem
from app.models.push import PushSubscription, Reminder
from app.models.user import User
from app.services import media, push
from app.services.slots import LABELS

logger = logging.getLogger(__name__)

# How late a reminder may still go out — a server that was down at 08:00 and
# back at 08:20 still reminds about breakfast; back at noon, it does not
GRACE = timedelta(minutes=30)

# A deleted meal can be brought back with Undo for this long
DELETED_MEALS_KEEP = timedelta(days=1)

DEFAULT_WATER_GOAL_ML = 2000

Sender = Callable[[str, str, str, dict], push.Outcome]


def zone_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def weekdays_of(mask: int) -> list[int]:
    return [day for day in range(7) if mask >> day & 1]


def mask_of(weekdays: list[int]) -> int:
    return sum(1 << day for day in set(weekdays))


def due_on(reminder: Reminder, now: datetime, zone: ZoneInfo) -> date | None:
    """The person's own date the reminder is due on now, or None."""
    if not reminder.enabled:
        return None
    local = now.astimezone(zone)
    if not reminder.weekdays >> local.weekday() & 1:
        return None
    scheduled = datetime.combine(local.date(), reminder.at, tzinfo=zone)
    if not scheduled <= local < scheduled + GRACE:
        return None
    if reminder.last_sent_on == local.date():
        return None
    return local.date()


def _kcal(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def _litres(ml: float) -> str:
    return f"{ml / 1000:.1f} l"


async def compose(session, reminder: Reminder, user: User, day: date) -> dict | None:
    """The notification for this reminder today — or None when there is
    nothing worth saying (the meal is logged, the water drunk)."""
    if reminder.kind == "meal":
        slot = reminder.slot or "snack"
        logged = await session.scalar(
            select(func.count())
            .select_from(Meal)
            .where(Meal.user_id == user.id, Meal.day == day, Meal.slot == slot)
        )
        if logged:
            return None
        label = LABELS.get(slot, "Meal")
        return {
            "title": f"{label} time",
            "body": f"What did you have for {label.lower()}? Logging it takes a few taps.",
            "url": f"/?add={slot}",
            "tag": f"reminder-meal-{slot}",
        }

    water = await session.scalar(
        select(func.coalesce(func.sum(WaterEntry.ml), 0)).where(
            WaterEntry.user_id == user.id, WaterEntry.day == day
        )
    )
    goal = user.water_goal_ml or DEFAULT_WATER_GOAL_ML

    if reminder.kind == "water":
        if water >= goal:
            return None
        return {
            "title": "Time for a glass of water",
            "body": f"{_litres(water)} of {_litres(goal)} so far today.",
            "url": "/",
            "tag": "reminder-water",
        }

    kcal = await session.scalar(
        select(func.coalesce(func.sum(MealItem.grams / 100 * MealItem.kcal100), 0))
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(Meal.user_id == user.id, Meal.day == day)
    )
    if not kcal:
        body = "Nothing logged today yet — anything you ate still counts."
    else:
        target = f" of {_kcal(user.target_kcal)}" if user.target_kcal else ""
        body = f"{_kcal(kcal)}{target} kcal and {_litres(water)} of water. Anything left to log?"
    return {"title": "Your day so far", "body": body, "url": "/", "tag": "reminder-summary"}


async def tick(now: datetime, sender: Sender = push.send) -> int:
    """Send everything due at `now`. Returns how many messages went out."""
    sent = 0
    async with database.SessionLocal() as session:
        has_browser = select(PushSubscription.id).where(PushSubscription.user_id == User.id)
        rows = (
            await session.execute(
                select(Reminder, User)
                .join(User, User.id == Reminder.user_id)
                .where(Reminder.enabled.is_(True), User.is_active.is_(True), has_browser.exists())
            )
        ).all()
        for reminder, user in rows:
            day = due_on(reminder, now, zone_of(user.timezone))
            if day is None:
                continue
            claimed = await session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder.id,
                    or_(Reminder.last_sent_on.is_(None), Reminder.last_sent_on != day),
                )
                .values(last_sent_on=day)
            )
            await session.commit()
            if claimed.rowcount != 1:
                continue
            message = await compose(session, reminder, user, day)
            if message is None:
                continue
            browsers = (
                await session.execute(
                    select(PushSubscription).where(PushSubscription.user_id == user.id)
                )
            ).scalars()
            for browser in list(browsers):
                outcome = await asyncio.to_thread(
                    sender, browser.endpoint, browser.p256dh, browser.auth, message
                )
                if outcome == "sent":
                    sent += 1
                elif outcome == "gone":
                    await session.delete(browser)
            await session.commit()
    return sent


async def tidy(now: datetime) -> None:
    """The housekeeping that rides on the same loop: meals deleted over a
    day ago are gone for good, and so are expired demo accounts — with
    everything in them, every user table cascades."""
    async with database.SessionLocal() as session:
        expired = now - DELETED_MEALS_KEEP
        await session.execute(delete(DeletedMeal).where(DeletedMeal.deleted_at < expired))
        demos = list(
            (
                await session.execute(
                    select(User.id).where(User.is_demo.is_(True), User.demo_expires_at < now)
                )
            ).scalars()
        )
        if demos:
            for user_id in demos:
                media.remove_previous(user_id)
            await session.execute(delete(User).where(User.id.in_(demos)))
            logger.info("Removed %s expired demo accounts", len(demos))
        await session.commit()


async def run(stop: asyncio.Event) -> None:
    """The loop itself, started with the app. A failing tick is logged and
    the next one tries again — reminders must never take the API down."""
    while not stop.is_set():
        now = datetime.now(UTC)
        try:
            if settings.push_enabled:
                await tick(now)
            await tidy(now)
        except Exception:  # noqa: BLE001
            logger.warning("Reminder loop tick failed", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.REMINDER_TICK_SECONDS)
