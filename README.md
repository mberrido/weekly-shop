# Weekly Shop

A small, phone-first meal planner and shopping list for the family, self-hosted
on a Synology NAS.

- **Meals:** the dinners, breakfasts and lunches you cook regularly, with
  ingredients. Type them in ("500 g chicken thighs") or import Thermomix
  recipes from Cookidoo by link, ID or search.
- **Week:** plan breakfast, lunch and dinner for each day, with per-day
  servings. You can copy last week, or pull your Cookidoo "My Week" into
  dinners.
- **List:** a merged, scaled shopping list grouped by UK supermarket aisle,
  showing which meals need each item. Tap to tick. You can share it as text,
  push it to your Cookidoo shopping list, remember aisle corrections and mark
  things you always have. The last list loaded stays viewable offline.
- Installable as a PWA, with dark mode, reduced-motion support and iPhone
  safe areas.
- **Pantry Tracker link (optional):** set `PANTRY_URL` and anything on the list
  that's in stock in [Pantry Tracker](../pantry-tracker) (quantity above 0)
  drops off the list and shows as an "In pantry" chip at the bottom. Names are
  matched automatically ('tuna' ↔ 'Tuna Chunks', but 'pasta' ≠ 'Pasta Bake').
  Tap an item or chip to link a different product or mark it "not in pantry".
  It's read-only; if the pantry is unreachable, the list is left alone.
- **Meal photos:** take one, pick from your library, or search online
  (Pexels, with the optional `PEXELS_API_KEY`). With a key, meals without a
  picture get the top result automatically, including existing ones on the
  next start. Removing a photo stops that meal being auto-filled again.
- A single 6-digit household PIN (see [DEPLOY.md](DEPLOY.md) for remote access).

Stack: Python 3.12, FastAPI, SQLite (plain `sqlite3`), and one static
`index.html` with vanilla JS. There's no build step. Cookidoo access uses the
unofficial [`cookidoo-api`](https://github.com/miaucl/cookidoo-api). If it
breaks or Cookidoo is down, those buttons show an error and everything else
keeps working.

## Layout

```
app/
  main.py              FastAPI app factory and API routes
  auth.py              password hash, signed session cookie, login lockout, security headers
  config.py            .env settings; refuses to start with weak/missing secrets
  db.py                schema, connections, daily snapshots
  parsing.py           quantity/unit parsing, normalisation, formatting
  aisles.py            aisle guessing (longest phrase wins)
  shopping.py          scaling, merging and grouping the list
  cookidoo_service.py  failure-tolerant Cookidoo wrapper (token reuse, 502s)
  importer.py          Cookidoo recipe → local meal
  pantry_client.py     read-only Pantry Tracker client (cached, fails soft)
  pantry_match.py      ingredient ↔ pantry product name matching
  static/              index.html, manifest, service worker, icons
tests/                 pytest suite (Cookidoo mocked; tiny in-repo ASGI client)
tools/make_icons.py    regenerates the PNG icons (stdlib only)
```

## Deployment

Every push to `main` runs the tests in GitHub Actions and publishes
`ghcr.io/mberrido/weekly-shop:latest` (plus a `sha-…` tag). The NAS runs
[`deploy/docker-compose.yml`](deploy/docker-compose.yml) and Watchtower pulls
updates. Full Synology steps are in [DEPLOY.md](DEPLOY.md).

## Local development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

cp .env.example .env
# set APP_PIN (6 digits), SESSION_SECRET (32+ bytes) and DB_PATH=./data/shop.db
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

.venv/bin/uvicorn --factory app.main:create_app --reload --port 8000
```

Open <http://localhost:8000> in Chrome or Firefox. They accept `Secure`
cookies on `localhost`; Safari doesn't, so use one of the others for local
work.

Run the tests:

```sh
.venv/bin/pytest -q
```

Run the container locally:

```sh
DATA_DIR=./data docker compose up -d --build     # then http://localhost:8420
```

## Notes

- Quantities: ranges use the upper bound (`1 - 2` → 2). Unicode and slash
  fractions and comma decimals are understood. kg/l are stored as g/ml and
  shown as kg/l from 1000 up.
- Items merge on a lowercased, singularised name plus unit, so "2 red onions"
  and "1 red onion" become "3".
- Recipes pushed to Cookidoo's list use Cookidoo's own quantities, which
  aren't scaled to your servings. Manual meals and extras go as scaled
  "additional items".
- The Cookidoo login token is saved to `data/cookidoo_token.json` (mode 600),
  so restarts don't log in again.
