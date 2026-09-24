"""Build the merged, aisle-grouped shopping list for a week."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from .aisles import AISLES, guess_aisle
from .pantry_match import best_match
from .parsing import format_qty, merge_key, normalise

_UNIT_ORDER = {"g": 0, "ml": 1}


def monday_of(value: str | date | None = None) -> date:
    d = date.fromisoformat(value) if isinstance(value, str) else (value or date.today())
    return d - timedelta(days=d.weekday())


@dataclass
class PlannedMeal:
    meal_name: str
    factor: float
    ingredients: list[dict]  # each: name, qty, unit
    source: str = "manual"
    meal_id: int | None = None
    plan_id: int | None = None


@dataclass
class Item:
    key: str
    name: str
    totals: dict[str, float] = field(default_factory=dict)
    some: bool = False
    meals: list[str] = field(default_factory=list)
    plan_ids: list[int] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """What this item is made of; changes if it's needed again after being deleted."""
        return ",".join(str(i) for i in sorted(self.plan_ids)) + "|" + self.qty

    @property
    def qty(self) -> str:
        parts = [
            format_qty(q, u)
            for u, q in sorted(self.totals.items(), key=lambda kv: (_UNIT_ORDER.get(kv[0], 2), kv[0]))
        ]
        if parts and self.some:
            parts.append("some")
        return " + ".join(parts)


def merge_items(planned: Iterable[PlannedMeal], pantry: set[str]) -> dict[str, Item]:
    """Scale, normalise and merge ingredients. Pantry keys are skipped."""
    items: dict[str, Item] = {}
    for pm in planned:
        for ing in pm.ingredients:
            key = merge_key(ing["name"])
            if not key or key in pantry:
                continue
            item = items.get(key)
            if item is None:
                item = items[key] = Item(key=key, name=ing["name"].strip())
            qty, unit = normalise(ing.get("qty"), ing.get("unit") or "")
            if qty is None:
                item.some = True
            else:
                item.totals[unit] = item.totals.get(unit, 0.0) + qty * pm.factor
            if pm.meal_name not in item.meals:
                item.meals.append(pm.meal_name)
            if pm.plan_id is not None and pm.plan_id not in item.plan_ids:
                item.plan_ids.append(pm.plan_id)
    return items


def drop_removed(conn: sqlite3.Connection, week_start: str, items: dict[str, Item]) -> dict[str, Item]:
    """Hide items deleted from this week's list, unless they've changed since (then forget the deletion)."""
    removed = dict(conn.execute("SELECT item_key, signature FROM removed WHERE week_start = ?", (week_start,)).fetchall())
    keep = {}
    for key, item in items.items():
        if key in removed:
            if removed[key] == item.signature:
                continue
            conn.execute("DELETE FROM removed WHERE week_start = ? AND item_key = ?", (week_start, key))
        keep[key] = item
    return keep


def planned_meals(conn: sqlite3.Connection, week_start: str) -> list[PlannedMeal]:
    rows = conn.execute(
        """SELECT p.id AS plan_id, p.meal_id, p.servings AS planned, m.name, m.servings, m.source
           FROM plan p JOIN meals m ON m.id = p.meal_id
           WHERE p.week_start = ? ORDER BY p.day, p.slot, p.id""",
        (week_start,),
    ).fetchall()
    ing_cache: dict[int, list[dict]] = {}
    out = []
    for r in rows:
        if r["meal_id"] not in ing_cache:
            ing_cache[r["meal_id"]] = [
                dict(x)
                for x in conn.execute(
                    "SELECT name, qty, unit FROM ingredients WHERE meal_id = ? ORDER BY position, id",
                    (r["meal_id"],),
                )
            ]
        base = r["servings"] or 1
        factor = (r["planned"] or base) / base
        out.append(PlannedMeal(r["name"], factor, ing_cache[r["meal_id"]], r["source"], r["meal_id"], r["plan_id"]))
    return out


def pantry_keys(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM pantry")}


def pantry_stock(conn: sqlite3.Connection, items: dict[str, Item], products: list[dict] | None) -> dict[str, dict]:
    """Which list items are in stock in Pantry Tracker: key -> {product, auto}.

    A saved link (or "not in pantry") wins; otherwise the best name match.
    In stock means quantity above 0.
    """
    if not products:
        return {}
    links = dict(conn.execute("SELECT name, product_id FROM pantry_links").fetchall())
    by_id = {p["id"]: p for p in products}
    stocked = [p for p in products if p["quantity"] > 0]
    out = {}
    for item in items.values():
        if item.key in links:
            product, auto = by_id.get(links[item.key]), False
        else:
            product, auto = best_match(item.name, stocked), True
        if product and product["quantity"] > 0:
            out[item.key] = {"product": product, "auto": auto}
    return out


def restock_signature(product: dict) -> str:
    """Changes when stock or the "add N" setting changes, so a deleted restock line can come back."""
    return f"{product['quantity']}|{product.get('weekly_shop_qty')}"


def low_products(products: list[dict] | None) -> list[dict]:
    """Pantry products that are low (at or below reorder level) and set to go on Weekly Shop."""
    return [p for p in (products or [])
            if p.get("weekly_shop_qty") and p["quantity"] <= p.get("reorder_threshold", 0)]


def build_list(conn: sqlite3.Connection, week_start: str, products: list[dict] | None = None,
               pantry_ok: bool | None = None, include_restock: bool = False) -> dict:
    """include_restock: add low Pantry Tracker items (only for the current week)."""
    items = merge_items(planned_meals(conn, week_start), pantry_keys(conn))
    stock = pantry_stock(conn, items, products)
    in_pantry = [
        {"key": item.key, "name": item.name, "qty": item.qty, "meals": item.meals,
         "product": stock[item.key]["product"], "auto": stock[item.key]["auto"]}
        for item in sorted(items.values(), key=lambda i: i.name.lower()) if item.key in stock
    ]
    items = drop_removed(conn, week_start, {k: v for k, v in items.items() if k not in stock})
    overrides = dict(conn.execute("SELECT name, aisle FROM aisles").fetchall())
    checked = {r[0] for r in conn.execute("SELECT item_key FROM checked WHERE week_start = ?", (week_start,))}

    groups: dict[str, list[dict]] = {a: [] for a in AISLES}
    for item in items.values():
        aisle = overrides.get(item.key) or guess_aisle(item.name)
        groups.setdefault(aisle, []).append(
            {"key": item.key, "name": item.name, "qty": item.qty, "meals": item.meals,
             "aisle": aisle, "checked": item.key in checked, "extra_id": None}
        )
    if include_restock:
        removed = dict(conn.execute("SELECT item_key, signature FROM removed WHERE week_start = ? AND item_key LIKE 'p:%'",
                                    (week_start,)).fetchall())
        for prod in low_products(products):
            key = f"p:{prod['id']}"
            if removed.get(key) == restock_signature(prod):
                continue
            aisle = overrides.get(merge_key(prod["name"])) or guess_aisle(prod["name"])
            groups.setdefault(aisle, []).append(
                {"key": key, "name": prod["name"], "qty": str(prod["weekly_shop_qty"]), "meals": [],
                 "aisle": aisle, "checked": key in checked, "extra_id": None,
                 "pantry_low": {"product_id": prod["id"], "quantity": prod["quantity"]}}
            )
    for r in conn.execute("SELECT id, name, qty FROM extras WHERE week_start = ? ORDER BY id", (week_start,)):
        key = f"x:{r['id']}"
        groups["Extras"].append(
            {"key": key, "name": r["name"], "qty": r["qty"], "meals": [], "aisle": "Extras",
             "checked": key in checked, "extra_id": r["id"]}
        )

    aisles = []
    for name, rows in groups.items():
        if rows:
            if name != "Extras":
                rows.sort(key=lambda x: x["name"].lower())
            aisles.append({"name": name, "items": rows})
    total = sum(len(a["items"]) for a in aisles)
    left = sum(1 for a in aisles for i in a["items"] if not i["checked"])
    return {"week_start": week_start, "aisles": aisles, "total": total, "left": left,
            "in_pantry": in_pantry, "pantry_ok": pantry_ok}
