"""
Reminders and the browsers they are delivered to.

`/push/*` is the Web Push plumbing: whether this server can send at all,
and the browsers (subscriptions) a person has said yes on. `/reminders` is
what they want to be reminded of and when. Both are the signed-in person's
own; someone else's reminder is a 404.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models.push import PushSubscription, Reminder
from app.schemas.push import (
    PushConfig,
    ReminderPatch,
    ReminderRead,
    ReminderWrite,
    SubscriptionDelete,
    SubscriptionWrite,
    TestResult,
)
from app.services import push, reminders, slots

router = APIRouter(tags=["reminders"])

MAX_REMINDERS = 20


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/push/config", response_model=PushConfig)
async def push_config(_: CurrentUser) -> PushConfig:
    """The key the browser subscribes with — or `enabled: false` while the
    server has none, so the page can say reminders are not set up here."""
    return PushConfig(
        enabled=settings.push_enabled,
        public_key=settings.VAPID_PUBLIC_KEY if settings.push_enabled else None,
    )


@router.post("/push/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(
    payload: SubscriptionWrite, request: Request, session: SessionDep, user: CurrentUser
) -> dict:
    if not push.is_push_endpoint(payload.endpoint):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That is not a browser push service address",
        )
    existing = (
        await session.execute(
            select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
        )
    ).scalar_one_or_none()
    # One browser belongs to whoever subscribed it last — a phone that
    # changed hands must not keep reminding the previous person's diary
    if existing is None:
        existing = PushSubscription(endpoint=payload.endpoint, user_id=user.id, p256dh="", auth="")
        session.add(existing)
    existing.user_id = user.id
    existing.p256dh = payload.keys.p256dh
    existing.auth = payload.keys.auth
    agent = request.headers.get("user-agent")
    existing.user_agent = agent[:512] if agent else None
    await session.flush()
    return {"id": str(existing.id)}


@router.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(payload: SubscriptionDelete, session: SessionDep, user: CurrentUser) -> None:
    existing = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == payload.endpoint, PushSubscription.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
        await session.flush()


@router.post("/push/test", response_model=TestResult)
async def send_test(session: SessionDep, user: CurrentUser) -> TestResult:
    """A notification right now, to every browser the person subscribed —
    the way to see that reminders will actually arrive."""
    if not settings.push_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reminders are not set up on this server",
        )
    browsers = list(
        (
            await session.execute(
                select(PushSubscription).where(PushSubscription.user_id == user.id)
            )
        ).scalars()
    )
    if not browsers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turn notifications on first"
        )
    message = {
        "title": "Reminders are on",
        "body": "This is how Nutrijurnal will nudge you.",
        "url": "/settings",
        "tag": "reminder-test",
    }
    result = TestResult()
    for browser in browsers:
        outcome = await asyncio.to_thread(
            push.send, browser.endpoint, browser.p256dh, browser.auth, message
        )
        if outcome == "sent":
            result.sent += 1
        else:
            result.failed += 1
            if outcome == "gone":
                await session.delete(browser)
    await session.flush()
    return result


def _read(reminder: Reminder) -> ReminderRead:
    return ReminderRead(
        id=reminder.id,
        kind=reminder.kind,
        slot=reminder.slot,
        at=reminder.at,
        weekdays=reminders.weekdays_of(reminder.weekdays),
        enabled=reminder.enabled,
        last_sent_on=reminder.last_sent_on,
    )


async def _own_reminder(session, user, reminder_id: uuid.UUID) -> Reminder:
    reminder = await session.get(Reminder, reminder_id)
    if reminder is None or reminder.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such reminder")
    return reminder


@router.get("/reminders", response_model=list[ReminderRead])
async def list_reminders(session: SessionDep, user: CurrentUser) -> list[ReminderRead]:
    rows = await session.execute(
        select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.at, Reminder.kind)
    )
    return [_read(reminder) for reminder in rows.scalars()]


@router.post("/reminders", response_model=ReminderRead, status_code=status.HTTP_201_CREATED)
async def create_reminder(
    payload: ReminderWrite, session: SessionDep, user: CurrentUser
) -> ReminderRead:
    count = len(
        (await session.execute(select(Reminder.id).where(Reminder.user_id == user.id))).all()
    )
    if count >= MAX_REMINDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{MAX_REMINDERS} reminders is the most — remove one first",
        )
    reminder = Reminder(
        user_id=user.id,
        kind=payload.kind,
        slot=(payload.slot or slots.slot_for_time(payload.at)) if payload.kind == "meal" else None,
        at=payload.at.replace(second=0, microsecond=0),
        weekdays=reminders.mask_of(payload.weekdays),
        enabled=payload.enabled,
    )
    session.add(reminder)
    await session.flush()
    return _read(reminder)


@router.patch("/reminders/{reminder_id}", response_model=ReminderRead)
async def update_reminder(
    reminder_id: uuid.UUID, payload: ReminderPatch, session: SessionDep, user: CurrentUser
) -> ReminderRead:
    reminder = await _own_reminder(session, user, reminder_id)
    fields = payload.model_dump(exclude_unset=True)
    at = payload.at.replace(second=0, microsecond=0) if fields.get("at") is not None else None
    if at is not None and at != reminder.at:
        reminder.at = at
        # A new time later today is a new appointment and may go out again;
        # one already behind the person's clock must not fire a second time
        # straight away for a day it was already sent on
        local = _now().astimezone(reminders.zone_of(user.timezone))
        if at > local.time():
            reminder.last_sent_on = None
    if fields.get("weekdays") is not None:
        reminder.weekdays = reminders.mask_of(payload.weekdays)
    if fields.get("enabled") is not None:
        reminder.enabled = payload.enabled
    if fields.get("slot") is not None and reminder.kind == "meal":
        reminder.slot = payload.slot
    await session.flush()
    return _read(reminder)


@router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reminder(reminder_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> None:
    reminder = await _own_reminder(session, user, reminder_id)
    await session.delete(reminder)
    await session.flush()
