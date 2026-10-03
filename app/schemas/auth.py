from datetime import date

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.user import UserRead


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("full_name")
    @classmethod
    def _a_name_is_more_than_spaces(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Enter your name")
        return name


class AccessToken(BaseModel):
    """The refresh token is not in here on purpose — it goes out as an
    httpOnly cookie so no JavaScript on the page can read it."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(AccessToken):
    user: UserRead


class MessageResponse(BaseModel):
    message: str


class ProvidersRead(BaseModel):
    """Which ways in this installation offers — the login screen asks before
    drawing a Google button that would only answer 501."""

    password: bool = True
    google: bool = False


class DemoRequest(BaseModel):
    """The viewer's own today, so the two weeks end on the day they are living
    in rather than the server's."""

    today: date | None = None


class DemoClaim(BaseModel):
    """Keeping a demo: the account becomes an ordinary one, diary and all."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("full_name")
    @classmethod
    def _a_name_is_more_than_spaces(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Enter your name")
        return name
