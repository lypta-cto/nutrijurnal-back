"""Importing every model here is what makes Alembic autogenerate see them."""

from app.models.base import Base
from app.models.eating import Food, Meal, MealItem, Recipe, RecipeItem
from app.models.refresh_token import RefreshToken
from app.models.user import Role, User

__all__ = [
    "Base",
    "Food",
    "Meal",
    "MealItem",
    "Recipe",
    "RecipeItem",
    "RefreshToken",
    "Role",
    "User",
]
