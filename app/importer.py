"""Import Cookidoo recipes into the local meal library."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .cookidoo_service import CookidooService
from .parsing import parse_amount, parse_line, split_note

_RECIPE_ID = re.compile(r"(?<![A-Za-z0-9])(r\d{2,})(?![A-Za-z0-9])")
_CUSTOM_URL = re.compile(r"created-recipes/[A-Za-z-]+/([A-Za-z0-9-]{6,})")
_CUSTOM_ID = re.compile(r"^[A-Za-z0-9-]{10,}$")


def parse_ref(text: str) -> tuple[str, str]:
    """Return ('recipe'|'custom', id) from an ID or a Cookidoo URL."""
    text = (text or "").strip()
    if m := _CUSTOM_URL.search(text):
        return "custom", m.group(1)
    if m := _RECIPE_ID.search(text):
        return "recipe", m.group(1)
    if _CUSTOM_ID.match(text):
        return "custom", text
    raise ValueError("That doesn't look like a Cookidoo recipe link or ID (e.g. r59322).")


def _insert_meal(conn: sqlite3.Connection, *, name: str, kind: str, source: str, cookidoo_id: str,
                 servings: int, url: str | None, image: str | None, ingredients: list[dict]) -> int:
    cur = conn.execute(
        "INSERT INTO meals(name, kind, source, cookidoo_id, servings, url, image) VALUES (?,?,?,?,?,?,?)",
        (name, kind, source, cookidoo_id, max(1, int(servings or 4)), url, image),
    )
    meal_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO ingredients(meal_id, position, name, qty, unit, note) VALUES (?,?,?,?,?,?)",
        [(meal_id, i, ing["name"], ing["qty"], ing["unit"], ing["note"]) for i, ing in enumerate(ingredients)
         if ing["name"]],
    )
    return meal_id


def existing_meal(conn: sqlite3.Connection, cookidoo_id: str) -> int | None:
    row = conn.execute("SELECT id FROM meals WHERE cookidoo_id = ?", (cookidoo_id,)).fetchone()
    return row["id"] if row else None


def recipe_to_ingredients(details: Any) -> list[dict]:
    out = []
    for ing in details.ingredients or []:
        name, note = split_note(ing.name or "")
        qty, unit, rest = parse_amount(ing.description or "")
        out.append({"name": name, "qty": qty, "unit": unit, "note": ", ".join(x for x in (rest, note) if x)})
    return out


def custom_to_ingredients(recipe: Any) -> list[dict]:
    return [parse_line(line) for line in (recipe.ingredients or []) if str(line).strip()]


async def import_ref(conn_factory, svc: CookidooService, kind_of_ref: str, cookidoo_id: str,
                     meal_kind: str) -> tuple[int, bool]:
    """Import one recipe; returns (meal_id, created). Skips if already imported.

    `conn_factory` is a context manager factory so the DB isn't held open
    while waiting on the network.
    """
    with conn_factory() as conn:
        if (mid := existing_meal(conn, cookidoo_id)) is not None:
            return mid, False
    if kind_of_ref == "recipe":
        d = await svc.recipe(cookidoo_id)
        fields = dict(name=d.name, source="cookidoo", servings=d.serving_size, url=d.url,
                      image=d.thumbnail or d.image, ingredients=recipe_to_ingredients(d))
    else:
        d = await svc.custom_recipe(cookidoo_id)
        fields = dict(name=d.name, source="cookidoo_custom", servings=d.serving_size, url=d.url,
                      image=d.thumbnail or d.image, ingredients=custom_to_ingredients(d))
    with conn_factory() as conn:
        if (mid := existing_meal(conn, cookidoo_id)) is not None:  # raced with another import
            return mid, False
        return _insert_meal(conn, kind=meal_kind, cookidoo_id=cookidoo_id, **fields), True
