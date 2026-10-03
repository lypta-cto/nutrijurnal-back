"""The connection string a host hands out works as pasted."""

import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("pasted", "used"),
    [
        # Supabase's Connect dialog (session pooler)
        (
            "postgresql://postgres.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
            "postgresql+asyncpg://postgres.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
        ),
        # The older scheme some hosts still print, with libpq's sslmode
        (
            "postgres://u:pw@db.example.com:5432/app?sslmode=require",
            "postgresql+asyncpg://u:pw@db.example.com:5432/app?ssl=require",
        ),
        # Already right, stray whitespace from a copy
        (
            "  postgresql+asyncpg://u:pw@localhost:5437/nutrijurnal \n",
            "postgresql+asyncpg://u:pw@localhost:5437/nutrijurnal",
        ),
    ],
)
def test_a_pasted_connection_string_is_read_with_asyncpg(pasted: str, used: str):
    settings = Settings(DATABASE_URL=pasted, SECRET_KEY="x" * 40, _env_file=None)
    assert str(settings.DATABASE_URL) == used
