# CLAUDE.md — nutrijurnal-back

FastAPI backend for **Nutrijurnal**, a public, mobile-first food diary anyone can sign up
for. Frontend: sibling repo `../nutrijurnal` (Nuxt SPA, port 3400). Product, architecture
and design docs live in **`../nutrijurnal/docs/`** — read `architecture.md` there before
larger changes, and add a line to its `changelog.md` when work lands.

Grown out of the eating module of the CTO Productivity App
(`../cto-productivity-app-backend`) on top of `admin-dashboard-template-back`.

## Ports

API **8004** · Postgres **5437** in docker (db `nutrijurnal`) · frontend **3400**. Offset
from the other local stacks so they all run side by side.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # uv is not installed
docker compose up -d db
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli dev        # http://localhost:8004, docs at /docs
.venv/bin/python -m app.cli seed       # shared foods (also loaded on every boot)
.venv/bin/python -m app.cli owner      # operator account from FIRST_SUPERUSER_*
.venv/bin/python -m app.cli secret     # SECRET_KEY for .env
.venv/bin/python -m app.cli vapid      # VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY for reminders

# The checks — green before any commit
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run on in-memory SQLite (foreign keys switched on, so cascades behave like
Postgres). Migrations: `alembic revision --autogenerate -m "…"` → read it → `upgrade`.
Migration files are kept as Alembic wrote them (ruff skips `alembic/versions`).

## Layout

`app/models` → `app/schemas` → `app/api/routes` → `app/services`.

- **auth / oauth** — open self-registration (`POST /auth/register`: email, password,
  full_name) answering with the same session as login. JWT access token (15 min, in
  memory on the client) + rotating refresh token (30 days, httpOnly cookie on
  `/api/v1/auth`, SHA-256 hash in `refresh_tokens`). `GET /auth/providers` tells the
  login screen whether Google is on (only when `GOOGLE_CLIENT_ID`/`SECRET` are set).
  Google admits only `GOOGLE_ALLOWED_EMAILS` (emails or `@domains`), checked before an
  account is created or linked — signing up with a password stays open to anyone.
  `DELETE /auth/me` closes an account; every user table cascades.
- **throttling** (`core/rate_limit.py`) — an in-process, per-worker limiter on the
  doors open without an account: sign-in per address (`LOGIN_PER_ADDRESS`, every try)
  and per email (`LOGIN_FAILURES_PER_EMAIL`, wrong passwords only), both over 15
  minutes; sign-up (`REGISTER_PER_HOUR`) and demos (`DEMO_PER_HOUR`) per address an
  hour. Past a limit: 429 with an English `detail` and `Retry-After`. The address is
  the connection's peer; `X-Forwarded-For` only with `TRUSTED_PROXY=true`, set only
  when every request comes through proxies that append to it — read
  `TRUSTED_PROXY_HOPS` entries from the end (2 in production: Vercel, then Render). Use `client_address(request)` wherever an address is recorded; tests
  start from `rate_limit.reset()` (autouse in conftest).
- **users** — admin-only CRUD kept from the template as operator tooling. Roles
  `viewer < member < admin < owner`; everyone who signs up is a member.
- **eating** (`/eating/*`) — the whole product:
  - `foods` hold nutrition per 100 g/ml. `user_id IS NULL` = shared, seeded from
    `app/data/foods_seed.json`, read-only for everyone (PATCH → 403). A food someone
    adds or scans has their `user_id` and is invisible to everyone else (404).
  - `recipes` + `recipe_items` — each person's own; a recipe can be known only by its
    `stated` numbers and still be eaten.
  - `meals` + `meal_items` — the diary. Items carry a COPY of the food's per-100 g
    numbers, so correcting a food never rewrites past days. An item may arrive with
    `macros` instead of a food ("quick kcal"): stored as unit `serving`, 100 g carrying
    the numbers, so quantity 2 is two servings.
  - Voice notes ride on `Meal.voice` (deferred column — only the play endpoint loads it).
  - Targets live on the user (`target_kcal/protein/carbs/fat`) with `onboarded_at`
    marking the first-run questions as done; `GET/PATCH /eating/settings` serves them.
  - `services/nutrition.py` reads written amounts ("1 merica whey", "malo putera",
    "two eggs", "0,5 l mleka", "piletina 200 g", "dvesta grama", "jedna i po banana")
    into grams on five-letter Serbian stems. A comma between two digits is a decimal,
    but three digits after a point or comma are thousands ("1.000 g") except in kg/l;
    a number before "%" is part of the name. Kilos, litres, decilitres and dekagrams
    (`SCALED_UNITS`) come back as grams and millilitres (the diary keeps only
    `models.eating.UNITS`, and converts a posted `kg`/`l`/`dl`/`dag` the same way). A
    portion ("porcija") is a piece, a glass of a drink, else 100 g. On a tie the food
    named first wins ("kafa sa mlekom" is coffee). A food-less line (quick kcal, a
    stated-only recipe) is unit `serving`: 100 g a serving at one serving's numbers.
  - Every write that names a day (`DiaryDay` in `schemas/eating.py`: meals, moves,
    copies, from-recipe, water, weight) refuses one more than a day past UTC's today
    with a 422 — Today never opens a day that hasn't come. A food's portion weighs at
    most `MAX_PORTION_GRAMS` (5000 g), and an Open Food Facts label past what a food
    can hold is a miss. `slots.py` places a meal in
    breakfast/lunch/dinner/snack and reads the slot out of a sentence ("za ručak");
    `food_lookup.py` reads a barcode (zxing-cpp) and asks Open Food Facts;
    `eating_report.py` prints the PDF/CSV export; `eating_seed.py` loads the shared foods,
    keyed so it is idempotent.
  - Meals carry a `slot`; `favourite_foods` stars; `GET /eating/foods/quick` gives the
    starred and recent foods with their last amounts; copies of a meal or a day keep the
    eaten numbers. A deleted meal is kept whole in `deleted_meals` for a day so
    `/restore` can undo it.
  - `services/goals.py` — the calculator behind `POST /eating/goals/estimate`; the
    answers live on the user (`sex`, `birth_year`, `height_cm`, `weight_kg`, `activity`,
    `goal`, `goal_pace`, `protein_per_kg`, `fat_percent`).
- **body** (`routes/body.py`, `models/body.py`) — `water_entries` (one per glass) and
  `weight_entries` (one per day); `routes/progress.py` answers a period in one request
  with the streak (`services/progress.py`).
- **reminders** (`routes/push.py`, `models/push.py`, `services/push.py`,
  `services/reminders.py`) — push subscriptions (only known push hosts), reminders in the
  user's `timezone`, and the in-process loop started in `main.py`'s lifespan that sends
  them and tidies up (deleted meals, expired demos).
- **demo** (`routes/demo.py`, `services/demo.py`) — `POST /auth/demo` builds a throwaway
  account with fourteen generic days; `is_demo`/`demo_expires_at` on the user; claim
  keeps it. **account** (`routes/account.py`) — `GET /auth/me/export` (a JSON backup;
  shared foods carry their `food_key`, since ids differ between databases) and
  `POST /auth/me/import` (`services/importer.py`): that backup, or a diary CSV — this
  app's or the CTO app's, same columns. Re-importing adds nothing twice; a file only
  ever links shared foods and the importer's own.

## Hard rules

- **Every query on user data filters on the current user.** Shared foods are global and
  read-only; foods a person creates or scans are private to them. New user-owned
  tables get `user_id` with `ondelete="CASCADE"` and a cross-user 404 test.
- Every user-visible string — API error `detail`s included — is English. Food names are
  data and stay Serbian. A database error never shows its cause: a broken constraint is
  a 409, an outage a 503 (the docker hint only when `ENVIRONMENT=local`).
- Uploads are read only up to their limit (`read(MAX + 1)`), images are sized from their
  header before decoding, and decoding runs in `asyncio.to_thread`.
- No cookbook or meal-plan recipes are ever seeded: those are third-party copyrighted or
  personal. Only `foods_seed.json` (generic per-100 g values) ships.
- Never read, print or copy any `.env`; `.env.example` is the reference. Never configure
  production databases, hosting or secrets here — deploy is done by the lead.
- Production is Render (`render.yaml`, Frankfurt) on Supabase Postgres, reached through
  the session pooler as plain Postgres — Supabase's client libraries and keys are not
  used. A pasted `postgresql://` string is switched to asyncpg in `config.py`.
  `alembic/env.py` turns row-level security on for every table in `public` after each
  upgrade: Supabase's Data API serves that schema to anyone with the public key, and
  RLS with no policy shuts it out while the app, as the owner, is unaffected.
- Never `git push`. Commit only your own paths (`git add <paths>`), imperative subject,
  a body that says why, ending with the `Co-Authored-By` line.
- Comments say why, not what. No dead code, no TODO litter.
