# Nutrijurnal API

The backend of **Nutrijurnal**, a public, mobile-first food diary: sign up, write down
what you ate (typed in plain words, picked from the pantry, scanned off a barcode, said
out loud, or cooked from your own recipe) and see the day add up against your targets.

FastAPI · SQLAlchemy 2 (async) + asyncpg · Alembic · Pydantic v2 · PyJWT · Authlib ·
Argon2 (pwdlib) · reportlab · zxing-cpp · pywebpush · pytest · ruff.

The frontend is the sibling repo [`nutrijurnal`](../nutrijurnal) (Nuxt, port 3400). Product
and architecture docs live there, in `docs/`.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m app.cli secret          # paste the output into SECRET_KEY in .env
docker compose up -d db                      # Postgres 17 on :5437, database "nutrijurnal"
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli dev              # http://localhost:8004 — docs at /docs
```

The shared foods load on boot. Then start the frontend next to it:

```bash
cd ../nutrijurnal && npm install && npm run dev   # http://localhost:3400
```

Ports are offset from Luka's other stacks (API **8004**, Postgres **5437**, frontend
**3400**) so all of them can run at once.

## Commands

```bash
.venv/bin/python -m app.cli dev      # API on the port the frontend expects
.venv/bin/python -m app.cli seed     # load / refresh the shared foods (idempotent by key)
.venv/bin/python -m app.cli owner    # create the operator account from FIRST_SUPERUSER_*
.venv/bin/python -m app.cli secret   # a fresh SECRET_KEY
.venv/bin/python -m app.cli vapid    # a fresh VAPID key pair for the reminders' Web Push

.venv/bin/python -m pytest -q        # runs on in-memory SQLite, no Postgres needed
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Migrations: `.venv/bin/alembic revision --autogenerate -m "…"`, read the file, then
`.venv/bin/alembic upgrade head`.

## Who sees what

The app is public and multi-tenant, so the rules are simple and enforced on every route:

| Data | Belongs to | Others can… |
| --- | --- | --- |
| Shared foods (`foods.user_id IS NULL`, seeded from `app/data/foods_seed.json`) | nobody | read them; nobody can edit them (403) |
| A food someone typed in or scanned | that person | not see it at all (404) |
| Recipes, meals, meal items, voice notes, targets | that person | not see them at all (404) |
| Starred foods, water, weight, reminders, push subscriptions, deleted-meal copies | that person | not see them at all (404) |

Deleting an account (`DELETE /auth/me`) takes every row of that person with it.
`GET /auth/me/export` hands all of it back as one JSON file.

## Background loop

With `REMINDERS_ENABLED` (default on) the app runs one in-process loop every
`REMINDER_TICK_SECONDS`: it sends the reminders that are due in each person's own
timezone (each one claimed with a conditional UPDATE, so several workers never send it
twice), forgets browsers the push service says are gone, purges meals deleted over a day
ago, and deletes expired demo accounts. Without `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`
reminders are kept but nothing is sent. Push endpoints are accepted only on the known
browser push services (FCM, Mozilla, Apple, Windows).

## Endpoints

All under `/api/v1`.

```
GET    /auth/providers               public — {password, google}: which sign-in buttons to draw
POST   /auth/register                email + password + full_name → session (refresh cookie + access token)
POST   /auth/login                   email + password → session
POST   /auth/refresh                 rotate the cookie → new access token
POST   /auth/logout                  revoke this session
GET    /auth/me · PATCH /auth/me     the signed-in person (UserRead carries onboarded_at)
DELETE /auth/me                      close the account and delete everything in it
POST   /auth/me/avatar · DELETE      profile photo
POST   /auth/me/password             change password (signs out everywhere)
GET    /auth/sessions · DELETE       where am I signed in / sign out everywhere
GET    /auth/google/authorize        → Google (501 unless GOOGLE_CLIENT_ID/SECRET are set)
GET    /auth/google/callback         ← Google; creates or links the account

GET    /eating/settings              targets, goal profile, water goal/glass, timezone, counts
PATCH  /eating/settings              targets, profile, water_goal_ml/glass_ml, timezone, onboarded: true
POST   /eating/goals/estimate        a body and a goal → kcal and macros (nothing saved)

GET    /eating/foods?q=&limit=&mine= shared + own foods, Serbian-aware search, starred/recent first
GET    /eating/foods/quick           starred + recently eaten foods, each with its last amount
POST   /eating/foods                 add your own food (per 100 g / ml)
PATCH  /eating/foods/{id}            edit your own food (shared ones → 403)
PUT    /eating/foods/{id}/favourite · DELETE   star / unstar any food you can see
GET    /eating/foods/barcode/{digits} the live scanner's digits → own food, shared, or Open Food Facts
POST   /eating/foods/scan            multipart `photo` → barcode → the same lookup
POST   /eating/parse                 "dodaj 200 g piletine za ručak" → items + the slot named

GET    /eating/days?from=&to=        per-day totals (≤ 400 days)
GET    /eating/days/{day}            the day: meals, totals, target, water, weight
POST   /eating/days/{day}/copy       another day's meals (or one slot of them) onto this day
POST   /eating/meals                 a meal with items; slot from the page, the time or the name
POST   /eating/meals/from-recipe     a recipe onto a day, scaled by servings
PATCH  /eating/meals/{id} · DELETE   delete keeps a copy for a day …
POST   /eating/meals/{id}/restore    … so Undo brings back the very same meal
POST   /eating/meals/{id}/copy       the same plate onto a day
POST   /eating/meals/{id}/items · PATCH/DELETE /eating/meals/{id}/items/{item_id}
POST   /eating/meals/{id}/voice      multipart `file`, `seconds`, `transcribed`
GET    /eating/meals/{id}/voice · DELETE

GET    /eating/recipes?q=            your recipes
POST   /eating/recipes · GET/PATCH/DELETE /eating/recipes/{id}

GET    /eating/water?from=&to= · GET /eating/water/{day} · POST /eating/water · DELETE /eating/water/{id}
GET    /eating/weight?from=&to= · GET /eating/weight/latest · PUT/DELETE /eating/weight/{day}
GET    /eating/progress?from=&to=&today=   every day's totals, water, weight, streak, averages
GET    /eating/export?from=&to=&format=pdf|csv

GET    /push/config                  {enabled, public_key} — the VAPID key browsers subscribe with
POST   /push/subscriptions · DELETE  this browser's subscription
POST   /push/test                    a notification now, to every subscribed browser
GET    /reminders · POST · PATCH/DELETE /reminders/{id}   meal / water / summary reminders

POST   /auth/demo                    public — a signed-in demo account with two weeks in it
POST   /auth/demo/claim              keep the demo: email + password + name
GET    /auth/me/export?recordings=   everything of yours as one JSON file

GET    /users …                      admin+ only — operator tooling (template reference CRUD)
GET    /health                       public — API and database status
```

## Auth in one paragraph

A 15-minute access token in the response body (the frontend keeps it in memory) and a
30-day refresh token in an `httpOnly` cookie scoped to `/api/v1/auth`, rotated on every
use and stored only as a SHA-256 hash. Registration is open to anyone and answers with
the same session shape as login. Google sign-in is wired but off until
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set; when on, it creates accounts for
anyone and links to an existing password account only when Google says the address is
verified.

## Not built yet

Email verification, password reset, rate limiting on login and registration (the demo has
its own per-address limit, in memory per worker). All are worth adding before real
traffic.
