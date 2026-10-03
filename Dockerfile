FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# Dependencies first, so editing code doesn't reinstall the world on every
# build. The build backend refuses to read the requirements unless the package
# directory exists, so it gets an empty one that is thrown away again — the
# real code arrives below and is installed without re-resolving anything.
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --upgrade pip \
    && pip install . \
    && rm -rf app

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN pip install --no-deps .

# Don't run as root
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /code
USER appuser

EXPOSE 8004

# $PORT because hosts tell the container where to listen; 8004 locally.
# Migrations run at boot — upgrade is idempotent.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8004}"]
