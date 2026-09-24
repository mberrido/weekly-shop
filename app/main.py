"""Weekly Shop: FastAPI app. Run with `uvicorn --factory app.main:create_app`."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db
from .aisles import AISLES
from .config import ConfigError, Settings, load_settings
from .cookidoo_service import CookidooError, CookidooService
from .importer import import_ref, legacy_kind, normalise_kinds, parse_ref
from .pantry_client import PantryClient
from .photo_search import PexelsClient, PhotoSearchError
from .parsing import merge_key
from .shopping import (build_list, drop_removed, low_products, merge_items, monday_of, pantry_keys, pantry_stock,
                       planned_meals, restock_signature)

log = logging.getLogger("weekly_shop")
STATIC = Path(__file__).parent / "static"
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_NAME = re.compile(r"^\d+-[0-9a-f]{16}\.jpg$")

Kind = Literal["breakfast", "lunch", "dinner", "any"]
Slot = Literal["breakfast", "lunch", "dinner"]


# -- request bodies ----------------------------------------------------------

class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: float | None = Field(default=None, ge=0, le=1_000_000)
    unit: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=200)


class MealIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kinds: list[Slot] | None = Field(default=None, min_length=1)   # meal times
    kind: Kind | None = None                                       # older single value; 'any' = all
    servings: int = Field(default=4, ge=1, le=50)
    url: str | None = Field(default=None, max_length=500)
    ingredients: list[IngredientIn] = Field(default_factory=list, max_length=200)


class PlanIn(BaseModel):
    week_start: date
    day: int = Field(ge=0, le=6)
    slot: Slot
    meal_id: int
    servings: int | None = Field(default=None, ge=1, le=50)


class PlanPatch(BaseModel):
    servings: int | None = Field(default=None, ge=1, le=50)


class WeekIn(BaseModel):
    week_start: date


class CheckIn(BaseModel):
    week_start: date
    key: str = Field(min_length=1, max_length=200)
    checked: bool


class RemoveIn(BaseModel):
    week_start: date
    key: str = Field(min_length=1, max_length=200)


class ExtraIn(BaseModel):
    week_start: date
    name: str = Field(min_length=1, max_length=120)
    qty: str = Field(default="", max_length=40)


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AisleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aisle: str | None = None


class PantryLinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    product_id: int | None = None   # None with auto=False means "not in pantry"
    auto: bool = False              # True clears the saved choice and goes back to auto-matching


class PexelsPickIn(BaseModel):
    photo_id: int = Field(gt=0)


class ImportIn(BaseModel):
    ref: str = Field(min_length=2, max_length=500)
    kinds: list[Slot] | None = Field(default=None, min_length=1)
    kind: Kind | None = "dinner"


# -- helpers -----------------------------------------------------------------

def _ws(d: date | str | None) -> str:
    try:
        return monday_of(d).isoformat()
    except ValueError:
        raise HTTPException(400, "Bad date; use YYYY-MM-DD.")


def _meal_rows(conn, where: str = "", args: tuple = ()) -> list[dict]:
    meals = [dict(r) for r in conn.execute(f"SELECT * FROM meals {where} ORDER BY name COLLATE NOCASE", args)]
    if not meals:
        return []
    ids = [m["id"] for m in meals]
    ings: dict[int, list[dict]] = {i: [] for i in ids}
    q = f"SELECT meal_id, name, qty, unit, note FROM ingredients WHERE meal_id IN ({','.join('?' * len(ids))}) ORDER BY position, id"
    for r in conn.execute(q, ids):
        ings[r["meal_id"]].append({k: r[k] for k in ("name", "qty", "unit", "note")})
    for m in meals:
        m["ingredients"] = ings[m["id"]]
        m["kinds"] = normalise_kinds([k for k in (m.get("kinds") or "").split(",") if k], m.get("kind"))
        m["photo_url"] = f"/photos/{m['photo']}" if m.get("photo") else None
    return meals


def _write_ingredients(conn, meal_id: int, ingredients: list[IngredientIn]) -> None:
    conn.execute("DELETE FROM ingredients WHERE meal_id = ?", (meal_id,))
    conn.executemany(
        "INSERT INTO ingredients(meal_id, position, name, qty, unit, note) VALUES (?,?,?,?,?,?)",
        [(meal_id, i, x.name.strip(), x.qty, x.unit.strip(), x.note.strip()) for i, x in enumerate(ingredients)],
    )


async def _backup_loop(db_path: str) -> None:
    while True:
        try:
            await asyncio.to_thread(db.backup, db_path)
        except Exception as e:
            log.warning("Database snapshot failed: %s", e)
        await asyncio.sleep(24 * 3600)


# -- app factory -------------------------------------------------------------

def create_app(settings: Settings | None = None, cookidoo: CookidooService | None = None,
               run_backups: bool = True, pantry: PantryClient | None = None,
               photos: PexelsClient | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings is None:
        from dotenv import load_dotenv
        load_dotenv()
        try:
            settings = load_settings()
        except ConfigError as e:
            log.error("%s", e)
            raise SystemExit(str(e)) from None
    db.init_db(settings.db_path)
    if cookidoo is None:
        cookidoo = CookidooService(
            settings.cookidoo_email, settings.cookidoo_password, settings.cookidoo_country,
            settings.cookidoo_language, settings.cookidoo_token_path,
        )

    if pantry is None:
        pantry = PantryClient(settings.pantry_url, settings.pantry_read_key)
    if photos is None:
        photos = PexelsClient(settings.pexels_api_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if run_backups:
            tasks.append(asyncio.create_task(_backup_loop(settings.db_path)))
            if photos.configured:
                tasks.append(asyncio.create_task(autofill_missing_photos(delay=3)))
        yield
        for task in tasks:
            task.cancel()
        await cookidoo.close()

    app = FastAPI(title="Weekly Shop", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.cookidoo = cookidoo
    auth.install(app, settings.app_pin, settings.session_secret)

    def conn():
        return db.session(settings.db_path)

    photos_dir = settings.data_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    def remove_photo_file(name: str | None) -> None:
        if name and PHOTO_NAME.match(name):
            (photos_dir / name).unlink(missing_ok=True)

    @app.exception_handler(PhotoSearchError)
    async def photo_search_error(_: Request, exc: PhotoSearchError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(CookidooError)
    async def cookidoo_error(_: Request, exc: CookidooError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    # -- pages & static -----------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with conn() as c:
            c.execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/icons", StaticFiles(directory=STATIC / "icons"), name="icons")

    # -- config -------------------------------------------------------------

    @app.get("/api/config")
    def config():
        return {"cookidoo": cookidoo.configured, "pantry": pantry.configured, "photos": photos.configured,
                "aisles": AISLES,
                "today": date.today().isoformat()}

    # -- meals --------------------------------------------------------------

    @app.get("/api/meals")
    def list_meals():
        with conn() as c:
            return _meal_rows(c)

    @app.get("/api/meals/{meal_id}")
    def get_meal(meal_id: int):
        with conn() as c:
            rows = _meal_rows(c, "WHERE id = ?", (meal_id,))
        if not rows:
            raise HTTPException(404, "Meal not found")
        return rows[0]

    @app.post("/api/meals", status_code=201)
    def create_meal(body: MealIn, background: BackgroundTasks, auto_photo: bool = True):
        with conn() as c:
            cur = c.execute(
                "INSERT INTO meals(name, kind, kinds, source, servings, url) VALUES (?,?,?,'manual',?,?)",
                (body.name.strip(), legacy_kind(kinds := normalise_kinds(body.kinds, body.kind)), ",".join(kinds),
                 body.servings, body.url),
            )
            _write_ingredients(c, cur.lastrowid, body.ingredients)
            meal = _meal_rows(c, "WHERE id = ?", (cur.lastrowid,))[0]
        if auto_photo and photos.configured:
            background.add_task(auto_photo_for, meal["id"])
        return meal

    @app.put("/api/meals/{meal_id}")
    def update_meal(meal_id: int, body: MealIn):
        with conn() as c:
            cur = c.execute(
                "UPDATE meals SET name = ?, kind = ?, kinds = ?, servings = ?, url = COALESCE(?, url) WHERE id = ?",
                (body.name.strip(), legacy_kind(kinds := normalise_kinds(body.kinds, body.kind)), ",".join(kinds),
                 body.servings, body.url, meal_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, "Meal not found")
            _write_ingredients(c, meal_id, body.ingredients)
            return _meal_rows(c, "WHERE id = ?", (meal_id,))[0]

    @app.delete("/api/meals/{meal_id}", status_code=204)
    def delete_meal(meal_id: int):
        with conn() as c:
            row = c.execute("SELECT photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
            c.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        remove_photo_file(row["photo"] if row else None)

    # -- meal photos (the browser resizes to a JPEG before uploading) --------

    @app.put("/api/meals/{meal_id}/photo")
    async def put_photo(meal_id: int, request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "image/jpeg":
            raise HTTPException(415, "Photos must be uploaded as JPEG.")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_PHOTO_BYTES:
                raise HTTPException(413, "That photo is too large.")
        if not body.startswith(b"\xff\xd8\xff"):
            raise HTTPException(400, "That doesn't look like a JPEG image.")
        with conn() as c:
            row = c.execute("SELECT photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Meal not found")
        return await save_photo(meal_id, bytes(body), None, row["photo"])

    async def save_photo(meal_id: int, data: bytes, credit: str | None, old: str | None) -> dict:
        name = f"{meal_id}-{secrets.token_hex(8)}.jpg"
        tmp = photos_dir / f".{name}.tmp"
        await asyncio.to_thread(tmp.write_bytes, data)
        os.replace(tmp, photos_dir / name)
        with conn() as c:
            c.execute("UPDATE meals SET photo = ?, photo_credit = ?, no_auto_photo = 0 WHERE id = ?",
                      (name, credit, meal_id))
            rows = _meal_rows(c, "WHERE id = ?", (meal_id,))
        remove_photo_file(old)
        if not rows:  # meal deleted meanwhile
            remove_photo_file(name)
            raise HTTPException(404, "Meal not found")
        return rows[0]

    # -- online photos (Pexels) ----------------------------------------------

    @app.get("/api/photo-search")
    async def photo_search(q: str = Query(min_length=2, max_length=100)):
        return await photos.search(q)

    @app.post("/api/meals/{meal_id}/photo/pexels")
    async def pick_pexels_photo(meal_id: int, body: PexelsPickIn):
        with conn() as c:
            row = c.execute("SELECT photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Meal not found")
        data, credit = await photos.download(body.photo_id)
        return await save_photo(meal_id, data, credit, row["photo"])

    async def auto_photo_for(meal_id: int) -> bool:
        """Give a meal the top Pexels result if it still has no picture. Never raises."""
        with conn() as c:
            m = c.execute("SELECT name, photo, image, no_auto_photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
        if not m or m["photo"] or m["image"] or m["no_auto_photo"]:
            return False
        try:
            hits = await photos.search(m["name"], per_page=1)
            if not hits:
                return False
            data, credit = await photos.download(hits[0]["id"])
            with conn() as c:  # re-check: the user may have added one meanwhile
                now = c.execute("SELECT photo, no_auto_photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if not now or now["photo"] or now["no_auto_photo"]:
                return False
            await save_photo(meal_id, data, credit, None)
            log.info("Added a Pexels photo for meal %s", meal_id)
            return True
        except (PhotoSearchError, HTTPException):
            return False

    async def autofill_missing_photos(delay: float = 0) -> int:
        await asyncio.sleep(delay)
        with conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM meals WHERE photo IS NULL AND (image IS NULL OR image = '') AND no_auto_photo = 0 ORDER BY id")]
        added = 0
        for meal_id in ids:
            added += await auto_photo_for(meal_id)
            await asyncio.sleep(1)  # stay well inside Pexels' rate limit
        return added

    app.state.autofill_missing_photos = autofill_missing_photos

    @app.delete("/api/meals/{meal_id}/photo")
    def delete_photo(meal_id: int):
        with conn() as c:
            row = c.execute("SELECT photo FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Meal not found")
            c.execute("UPDATE meals SET photo = NULL, photo_credit = NULL, no_auto_photo = 1 WHERE id = ?", (meal_id,))
            meal = _meal_rows(c, "WHERE id = ?", (meal_id,))[0]
        remove_photo_file(row["photo"])
        return meal

    @app.get("/photos/{name}", include_in_schema=False)
    def get_photo(name: str):
        path = photos_dir / name
        if not PHOTO_NAME.match(name) or not path.is_file():
            raise HTTPException(404, "Not found")
        # Names are random and never reused, so the browser can cache them for good.
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    # -- week plan ----------------------------------------------------------

    @app.get("/api/week")
    def get_week(start: str | None = Query(default=None)):
        ws = _ws(start)
        with conn() as c:
            rows = c.execute(
                """SELECT p.id, p.day, p.slot, p.meal_id, p.servings AS planned_servings,
                          m.name, m.image, m.kind, m.servings AS meal_servings
                   FROM plan p JOIN meals m ON m.id = p.meal_id
                   WHERE p.week_start = ? ORDER BY p.day, p.id""",
                (ws,),
            ).fetchall()
        monday = date.fromisoformat(ws)
        entries = [dict(r) | {"servings": r["planned_servings"] or r["meal_servings"]} for r in rows]
        return {
            "week_start": ws,
            "days": [(monday + timedelta(days=i)).isoformat() for i in range(7)],
            "entries": entries,
        }

    @app.post("/api/plan", status_code=201)
    def add_plan(body: PlanIn):
        with conn() as c:
            if not c.execute("SELECT 1 FROM meals WHERE id = ?", (body.meal_id,)).fetchone():
                raise HTTPException(404, "Meal not found")
            cur = c.execute(
                "INSERT INTO plan(week_start, day, slot, meal_id, servings) VALUES (?,?,?,?,?)",
                (_ws(body.week_start), body.day, body.slot, body.meal_id, body.servings),
            )
            return {"id": cur.lastrowid}

    @app.patch("/api/plan/{plan_id}")
    def patch_plan(plan_id: int, body: PlanPatch):
        with conn() as c:
            cur = c.execute("UPDATE plan SET servings = ? WHERE id = ?", (body.servings, plan_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Not found")
        return {"ok": True}

    @app.delete("/api/plan/{plan_id}", status_code=204)
    def delete_plan(plan_id: int):
        with conn() as c:
            c.execute("DELETE FROM plan WHERE id = ?", (plan_id,))

    @app.post("/api/week/copy-last")
    def copy_last_week(body: WeekIn):
        ws = _ws(body.week_start)
        prev = (date.fromisoformat(ws) - timedelta(days=7)).isoformat()
        with conn() as c:
            cur = c.execute(
                """INSERT INTO plan(week_start, day, slot, meal_id, servings)
                   SELECT ?, p.day, p.slot, p.meal_id, p.servings FROM plan p
                   WHERE p.week_start = ? AND NOT EXISTS (
                       SELECT 1 FROM plan q WHERE q.week_start = ? AND q.day = p.day
                         AND q.slot = p.slot AND q.meal_id = p.meal_id)
                   ORDER BY p.id""",
                (ws, prev, ws),
            )
            return {"copied": cur.rowcount}

    # -- shopping list ------------------------------------------------------

    @app.get("/api/list")
    async def get_list(week: str | None = Query(default=None)):
        ws = _ws(week)
        products = await pantry.products()
        ok = (products is not None) if pantry.configured else None
        with conn() as c:
            return build_list(c, ws, products, ok, include_restock=ws == monday_of().isoformat())

    # -- Pantry Tracker (read-only) ------------------------------------------

    @app.get("/api/pantry-products")
    async def pantry_products():
        products = await pantry.products()
        return {"configured": pantry.configured, "ok": products is not None,
                "products": sorted(products or [], key=lambda p: p["name"].lower())}

    @app.put("/api/pantry-links")
    def set_pantry_link(body: PantryLinkIn):
        key = merge_key(body.name)
        with conn() as c:
            if body.auto:
                c.execute("DELETE FROM pantry_links WHERE name = ?", (key,))
            else:
                c.execute("INSERT INTO pantry_links(name, product_id) VALUES (?, ?) "
                          "ON CONFLICT(name) DO UPDATE SET product_id = excluded.product_id", (key, body.product_id))
        return {"ok": True}

    @app.post("/api/list/check")
    def check_item(body: CheckIn):
        ws = _ws(body.week_start)
        with conn() as c:
            if body.checked:
                c.execute("INSERT OR IGNORE INTO checked(week_start, item_key) VALUES (?,?)", (ws, body.key))
            else:
                c.execute("DELETE FROM checked WHERE week_start = ? AND item_key = ?", (ws, body.key))
        return {"ok": True}

    def remove_restock(c, ws: str, key: str, products: list[dict] | None) -> bool:
        """Hide a low-pantry line until its stock or setting changes."""
        prod = next((p for p in low_products(products) if f"p:{p['id']}" == key), None)
        if prod is None:
            return False
        c.execute("INSERT INTO removed(week_start, item_key, signature) VALUES (?,?,?) "
                  "ON CONFLICT(week_start, item_key) DO UPDATE SET signature = excluded.signature",
                  (ws, key, restock_signature(prod)))
        return True

    @app.post("/api/list/remove")
    async def remove_item(body: RemoveIn):
        """Delete an item from this week's list. Extras are deleted outright; meal
        ingredients are hidden until something changes (see shopping.drop_removed)."""
        ws = _ws(body.week_start)
        products = await pantry.products() if body.key.startswith("p:") else None
        with conn() as c:
            if body.key.startswith("x:"):
                c.execute("DELETE FROM extras WHERE id = ? AND week_start = ?", (body.key[2:], ws))
            elif body.key.startswith("p:"):
                if not remove_restock(c, ws, body.key, products):
                    raise HTTPException(404, "That item isn't on this week's list.")
            else:
                item = merge_items(planned_meals(c, ws), pantry_keys(c)).get(body.key)
                if item is None:
                    raise HTTPException(404, "That item isn't on this week's list.")
                c.execute("INSERT INTO removed(week_start, item_key, signature) VALUES (?,?,?) "
                          "ON CONFLICT(week_start, item_key) DO UPDATE SET signature = excluded.signature",
                          (ws, body.key, item.signature))
            c.execute("DELETE FROM checked WHERE week_start = ? AND item_key = ?", (ws, body.key))
        return {"ok": True}

    @app.post("/api/list/empty-basket")
    async def empty_basket(body: WeekIn):
        """Take every ticked item off this week's list (they come back if needed again, like Delete)."""
        ws = _ws(body.week_start)
        products = await pantry.products()
        with conn() as c:
            ticked = [r[0] for r in c.execute("SELECT item_key FROM checked WHERE week_start = ?", (ws,))]
            items = merge_items(planned_meals(c, ws), pantry_keys(c))
            for key in ticked:
                if key.startswith("x:"):
                    c.execute("DELETE FROM extras WHERE id = ? AND week_start = ?", (key[2:], ws))
                elif key.startswith("p:"):
                    remove_restock(c, ws, key, products)
                elif key in items:
                    c.execute("INSERT INTO removed(week_start, item_key, signature) VALUES (?,?,?) "
                              "ON CONFLICT(week_start, item_key) DO UPDATE SET signature = excluded.signature",
                              (ws, key, items[key].signature))
            c.execute("DELETE FROM checked WHERE week_start = ?", (ws,))
        return {"emptied": len(ticked)}

    @app.post("/api/list/untick-all")
    def untick_all(body: WeekIn):
        with conn() as c:
            c.execute("DELETE FROM checked WHERE week_start = ?", (_ws(body.week_start),))
        return {"ok": True}

    @app.post("/api/extras", status_code=201)
    def add_extra(body: ExtraIn):
        with conn() as c:
            cur = c.execute("INSERT INTO extras(week_start, name, qty) VALUES (?,?,?)",
                            (_ws(body.week_start), body.name.strip(), body.qty.strip()))
            return {"id": cur.lastrowid}

    @app.delete("/api/extras/{extra_id}", status_code=204)
    def delete_extra(extra_id: int):
        with conn() as c:
            c.execute("DELETE FROM extras WHERE id = ?", (extra_id,))
            c.execute("DELETE FROM checked WHERE item_key = ?", (f"x:{extra_id}",))

    # -- pantry & aisles ----------------------------------------------------

    @app.get("/api/pantry")
    def get_pantry():
        with conn() as c:
            return [r[0] for r in c.execute("SELECT name FROM pantry ORDER BY name")]

    @app.post("/api/pantry", status_code=201)
    def add_pantry(body: NameIn):
        key = merge_key(body.name)
        if not key:
            raise HTTPException(400, "Name required")
        with conn() as c:
            c.execute("INSERT OR IGNORE INTO pantry(name) VALUES (?)", (key,))
        return {"name": key}

    @app.delete("/api/pantry/{name}", status_code=204)
    def delete_pantry(name: str):
        with conn() as c:
            c.execute("DELETE FROM pantry WHERE name = ?", (merge_key(name),))

    @app.put("/api/aisles")
    def set_aisle(body: AisleIn):
        key = merge_key(body.name)
        if body.aisle is not None and body.aisle not in AISLES:
            raise HTTPException(400, "Unknown aisle")
        with conn() as c:
            if body.aisle:
                c.execute("INSERT INTO aisles(name, aisle) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET aisle = excluded.aisle",
                          (key, body.aisle))
            else:
                c.execute("DELETE FROM aisles WHERE name = ?", (key,))
        return {"ok": True}

    # -- Cookidoo -----------------------------------------------------------

    @app.get("/api/cookidoo/search")
    async def cookidoo_search(q: str = Query(min_length=2, max_length=100)):
        result = await cookidoo.search(q)
        return [{"id": r.id, "name": r.name, "thumbnail": r.thumbnail, "url": r.url} for r in result.recipes]

    @app.post("/api/cookidoo/import")
    async def cookidoo_import(body: ImportIn):
        try:
            ref_kind, rid = parse_ref(body.ref)
        except ValueError as e:
            raise HTTPException(400, str(e))
        meal_id, created = await import_ref(conn, cookidoo, ref_kind, rid, normalise_kinds(body.kinds, body.kind))
        with conn() as c:
            meal = _meal_rows(c, "WHERE id = ?", (meal_id,))[0]
        return {"meal": meal, "created": created}

    @app.post("/api/cookidoo/pull-week")
    async def cookidoo_pull_week(body: WeekIn):
        ws = _ws(body.week_start)
        monday = date.fromisoformat(ws)
        days = await cookidoo.calendar_week(monday)
        added = imported = skipped = 0
        for d in days:
            try:
                idx = (date.fromisoformat(str(d.id)[:10]) - monday).days
            except ValueError:
                continue
            if not 0 <= idx <= 6:
                continue
            refs = [("recipe", r.id) for r in (d.recipes or [])]
            refs += [("custom", cid) for cid in (getattr(d, "customer_recipe_ids", None) or [])]
            for ref_kind, rid in refs:
                meal_id, created = await import_ref(conn, cookidoo, ref_kind, rid, ["dinner"])
                imported += created
                with conn() as c:
                    exists = c.execute(
                        "SELECT 1 FROM plan WHERE week_start = ? AND day = ? AND slot = 'dinner' AND meal_id = ?",
                        (ws, idx, meal_id),
                    ).fetchone()
                    if exists:
                        skipped += 1
                    else:
                        c.execute("INSERT INTO plan(week_start, day, slot, meal_id) VALUES (?,?, 'dinner', ?)",
                                  (ws, idx, meal_id))
                        added += 1
        return {"added": added, "imported": imported, "skipped": skipped}

    @app.post("/api/cookidoo/send-list")
    async def cookidoo_send_list(body: WeekIn):
        ws = _ws(body.week_start)
        products = await pantry.products()
        with conn() as c:
            planned = planned_meals(c, ws)
            ids = c.execute(
                """SELECT DISTINCT m.source, m.cookidoo_id FROM plan p JOIN meals m ON m.id = p.meal_id
                   WHERE p.week_start = ? AND m.cookidoo_id IS NOT NULL ORDER BY p.id""",
                (ws,),
            ).fetchall()
            checked = {r[0] for r in c.execute("SELECT item_key FROM checked WHERE week_start = ?", (ws,))}
            manual = merge_items([p for p in planned if p.source == "manual"], pantry_keys(c))
            in_stock = pantry_stock(c, manual, products)
            everything = merge_items(planned, pantry_keys(c))   # deletions are recorded against the full list
            deleted = set(everything) - set(drop_removed(c, ws, everything))
            manual = {k: v for k, v in manual.items() if k not in in_stock and k not in deleted}
            lines = [f"{i.qty} {i.name}".strip() for i in manual.values() if i.key not in checked]
            lines += [f"{r['qty']} {r['name']}".strip() for r in
                      c.execute("SELECT id, name, qty FROM extras WHERE week_start = ? ORDER BY id", (ws,))
                      if f"x:{r['id']}" not in checked]
        recipe_ids = [r["cookidoo_id"] for r in ids if r["source"] == "cookidoo"]
        custom_ids = [r["cookidoo_id"] for r in ids if r["source"] == "cookidoo_custom"]
        if not (recipe_ids or custom_ids or lines):
            raise HTTPException(400, "Nothing to send for this week.")
        await cookidoo.push_list(recipe_ids, custom_ids, lines)
        return {"recipes": len(recipe_ids) + len(custom_ids), "items": len(lines)}

    return app
