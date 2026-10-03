import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import Role


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)
    role: Role = Role.MEMBER


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=1024)


class UserAdminUpdate(UserUpdate):
    """Fields only an admin may change."""

    role: Role | None = None
    is_active: bool | None = None


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Role
    avatar_url: str | None
    is_active: bool
    is_verified: bool
    has_password: bool
    created_at: datetime
    # Null until the first-run questions are answered — the frontend's cue
    # to open /onboarding before anything else
    onboarded_at: datetime | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class PageMeta(BaseModel):
    total: int
    page: int
    page_size: int


class UserPage(BaseModel):
    items: list[UserRead]
    meta: PageMeta
