"""Importing every model here is what makes Alembic autogenerate see them."""

from app.models.base import Base
from app.models.body import WaterEntry, WeightEntry
from app.models.eating import (
    DeletedMeal,
    FavouriteFood,
    Food,
    Meal,
    MealItem,
    Recipe,
    RecipeItem,
)
from app.models.push import PushSubscription, Reminder
from app.models.refresh_token import RefreshToken
from app.models.user import Role, User

__all__ = [
    "Base",
    "DeletedMeal",
    "FavouriteFood",
    "Food",
    "Meal",
    "MealItem",
    "Recipe",
    "PushSubscription",
    "RecipeItem",
    "RefreshToken",
    "Reminder",
    "Role",
    "User",
    "WaterEntry",
    "WeightEntry",
]
