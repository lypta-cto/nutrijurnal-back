"""
Everything a person has put into Nutrijurnal, handed back as one file.

The diary already prints as a PDF or a CSV (`/eating/export`); this is the
rest of it too, in a form another program can read: the account and its
targets, every meal with the numbers it was eaten at, their own foods and
recipes, the stars, water, weight and reminders. Voice recordings are
large, so they come along only when asked for. The browsers subscribed to
reminders are left out — their addresses are the push services' business.
"""

import base64
import json
import uuid
from datetime import UTC, date, datetime, time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.api.deps import CurrentUser, SessionDep
from app.models.body import WaterEntry, WeightEntry
from app.models.eating import FavouriteFood, Food, Meal, Recipe
from app.models.push import Reminder
from app.services import goals, reminders

router = APIRouter(prefix="/auth", tags=["auth"])

FORMAT_VERSION = 1


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row(entity, fields: tuple[str, ...]) -> dict:
    return {name: _plain(getattr(entity, name)) for name in fields}


@router.get("/me/export")
async def export_everything(
    session: SessionDep,
    user: CurrentUser,
    recordings: bool = Query(default=False, description="Include voice recordings, base64"),
) -> Response:
    async def rows(statement):
        return list((await session.execute(statement)).scalars())

    meals_query = select(Meal).where(Meal.user_id == user.id).order_by(Meal.day, Meal.at)
    if recordings:
        meals_query = meals_query.options(undefer(Meal.voice))
    meals = await rows(meals_query)
    foods = await rows(select(Food).where(Food.user_id == user.id).order_by(Food.name))
    recipes = await rows(select(Recipe).where(Recipe.user_id == user.id).order_by(Recipe.title))
    starred = await rows(select(FavouriteFood).where(FavouriteFood.user_id == user.id))
    water = await rows(
        select(WaterEntry).where(WaterEntry.user_id == user.id).order_by(WaterEntry.created_at)
    )
    weight = await rows(
        select(WeightEntry).where(WeightEntry.user_id == user.id).order_by(WeightEntry.day)
    )
    wanted = await rows(select(Reminder).where(Reminder.user_id == user.id))

    def meal_of(meal: Meal) -> dict:
        body = _row(meal, ("id", "day", "at", "slot", "title", "recipe_title", "servings", "note"))
        body["items"] = [
            _row(
                item,
                (
                    "food_id",
                    "label",
                    "quantity",
                    "unit",
                    "grams",
                    "kcal100",
                    "protein100",
                    "carbs100",
                    "fat100",
                ),
            )
            | {"macros": item.macros}
            for item in meal.items
        ]
        if meal.voice_type:
            body["voice"] = {
                "type": meal.voice_type,
                "seconds": meal.voice_seconds,
                "transcribed": meal.voice_transcribed,
            }
            if recordings and meal.voice:
                body["voice"]["base64"] = base64.b64encode(meal.voice).decode()
        return body

    profile = goals.profile_of(user)
    document = {
        "app": "Nutrijurnal",
        "format": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "account": {
            **_row(
                user,
                (
                    "email",
                    "full_name",
                    "created_at",
                    "onboarded_at",
                    "timezone",
                    "target_kcal",
                    "target_protein",
                    "target_carbs",
                    "target_fat",
                    "water_goal_ml",
                    "water_glass_ml",
                ),
            ),
            "goal_profile": profile.model_dump() if profile else None,
        },
        "meals": [meal_of(meal) for meal in meals],
        "foods": [
            _row(
                food,
                (
                    "id",
                    "name",
                    "brand",
                    "kcal",
                    "protein",
                    "carbs",
                    "fat",
                    "base_unit",
                    "units",
                    "barcode",
                    "source",
                    "archived",
                ),
            )
            for food in foods
        ],
        "starred_foods": [str(row.food_id) for row in starred],
        "recipes": [
            _row(
                recipe,
                (
                    "id",
                    "title",
                    "subtitle",
                    "servings",
                    "serving_unit",
                    "minutes",
                    "steps",
                    "note",
                    "stated",
                ),
            )
            | {
                "items": [
                    _row(item, ("food_id", "label", "quantity", "unit", "grams", "optional"))
                    for item in recipe.items
                ]
            }
            for recipe in recipes
        ],
        "water": [_row(entry, ("day", "ml", "created_at")) for entry in water],
        "weight": [_row(entry, ("day", "kg")) for entry in weight],
        "reminders": [
            _row(reminder, ("kind", "slot", "at", "enabled"))
            | {"weekdays": reminders.weekdays_of(reminder.weekdays)}
            for reminder in wanted
        ],
    }
    name = f"Nutrijurnal_export_{datetime.now(UTC):%Y-%m-%d}.json"
    return Response(
        content=json.dumps(document, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
