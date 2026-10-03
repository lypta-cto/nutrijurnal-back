import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.models import Base  # noqa: F401 — importing registers every model

config = context.config
config.set_main_option("sqlalchemy.url", str(settings.DATABASE_URL))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings.DATABASE_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # catches column type changes in autogenerate
    )

    with context.begin_transaction():
        context.run_migrations()
        close_the_data_api(connection)


def close_the_data_api(connection: Connection) -> None:
    """Row-level security on, with no policies, for every table in public.

    Supabase serves the public schema through its Data API to anyone holding
    the project's publishable key, which is public by design — and tables
    Alembic creates have row-level security off, so every row (password
    hashes included) would be one HTTP call away. With it on and no policy,
    that API sees nothing. The app is unaffected: it connects as the tables'
    owner, whom row-level security does not restrict. Done after every
    upgrade, so a table added later is covered without anyone remembering.
    Harmless on a plain Postgres.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(
        """
        DO $$
        DECLARE t record;
        BEGIN
          FOR t IN
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity
          LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.relname);
          END LOOP;
        END $$;
        """
    )


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
