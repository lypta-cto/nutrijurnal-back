from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Every knob the app has. Values come from the environment or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App -----------------------------------------------------------------
    PROJECT_NAME: str = "Nutrijurnal API"
    API_V1_PREFIX: str = "/api/v1"

    # Used by `python -m app.cli dev` so the port can't drift from whatever
    # the frontend is configured to call.
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8004
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False

    # --- Database ------------------------------------------------------------
    # Neon: postgresql+asyncpg://user:pass@ep-xxx.region.aws.neon.tech/dbname
    DATABASE_URL: PostgresDsn

    # --- Security ------------------------------------------------------------
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    SECRET_KEY: str = Field(min_length=32)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    ALGORITHM: str = "HS256"

    # --- Cookies -------------------------------------------------------------
    # The refresh token lives in an httpOnly cookie the frontend never reads
    REFRESH_COOKIE_NAME: str = "refresh_token"
    COOKIE_SECURE: bool = False  # must be True in production (HTTPS only)
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    COOKIE_DOMAIN: str | None = None

    # --- CORS ----------------------------------------------------------------
    FRONTEND_URL: str = "http://localhost:3400"

    # NoDecode stops pydantic-settings from trying to JSON-parse this, so the
    # .env can hold a plain comma-separated list instead of a JSON array.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3400"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def cors_origin_regex(self) -> str | None:
        """Locally the dev server's port moves around, so allow any localhost
        port. Never active outside ENVIRONMENT=local."""
        if self.ENVIRONMENT == "local":
            return r"http://(localhost|127\.0\.0\.1):\d+"
        return None

    # --- Google OAuth --------------------------------------------------------
    # Create at https://console.cloud.google.com/apis/credentials
    # Authorised redirect URI: {BACKEND_URL}/api/v1/auth/google/callback
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    BACKEND_URL: str = "http://localhost:8004"

    @property
    def google_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.BACKEND_URL}{self.API_V1_PREFIX}/auth/google/callback"

    # --- Uploads -------------------------------------------------------------
    # Local disk. Swap app/services/media.py for S3/R2 when you scale past one
    # machine — the rest of the app only ever sees the returned URL.
    UPLOAD_DIR: str = "uploads"
    UPLOAD_URL_PREFIX: str = "/uploads"
    MAX_AVATAR_BYTES: int = 5 * 1024 * 1024

    # --- Seed ----------------------------------------------------------------
    # The shared foods are loaded on every boot, so a fresh database has a
    # pantry without anyone remembering a command. Keyed, so it only ever
    # updates what is there. Turn off where several workers start at once and
    # run `python -m app.cli seed` from the deploy step instead.
    SEED_FOODS_ON_STARTUP: bool = True

    # --- Operator account ----------------------------------------------------
    # Created by `python -m app.cli owner` — the one account that may use the
    # /users admin routes. Everyone else signs up through /auth/register.
    FIRST_SUPERUSER_EMAIL: str = "admin@example.com"
    FIRST_SUPERUSER_PASSWORD: str = "changeme123"


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is read once per process."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
