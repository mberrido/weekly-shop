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
    def qty(self) -> str:
        parts = [
            format_qty(q, u)
            for u, q in sorted(self.totals.items(), key=lambda kv: (_UNIT_ORDER.get(kv[0], 2), kv[0]))
        ]
        if parts and self.some:
            parts.append("some")
        return " + ".join(parts)


def merge_items(planned: Iterable[PlannedMeal], pantry: set[str],
                bought: set[tuple[int, str]] | frozenset = frozenset()) -> dict[str, Item]:
    """Scale, normalise and merge ingredients. Pantry keys and bought (plan, item) pairs are skipped."""
    items: dict[str, Item] = {}
    for pm in planned:
        for ing in pm.ingredients:
            key = merge_key(ing["name"])
            if not key or key in pantry or (pm.plan_id, key) in bought:
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


LIST = "list"   # the one shopping list: key used for ticks, extras and hidden pantry lines


def bought_pairs(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    return {(r[0], r[1]) for r in conn.execute("SELECT plan_id, item_key FROM bought")}


def current_items(conn: sqlite3.Connection) -> dict[str, Item]:
    """Ingredients still to buy, from every planned meal in every week."""
    return merge_items(planned_meals(conn), pantry_keys(conn), bought_pairs(conn))


def planned_meals(conn: sqlite3.Connection, week_start: str | None = None) -> list[PlannedMeal]:
    """Planned meals for one week, or for every week when week_start is None."""
    where, args = ("WHERE p.week_start = ?", (week_start,)) if week_start else ("", ())
    rows = conn.execute(
        f"""SELECT p.id AS plan_id, p.meal_id, p.servings AS planned, m.name, m.servings, m.source
            FROM plan p JOIN meals m ON m.id = p.meal_id
            {where} ORDER BY p.week_start, p.day, p.slot, p.id""",
        args,
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


def build_list(conn: sqlite3.Connection, products: list[dict] | None = None,
               pantry_ok: bool | None = None) -> dict:
    """The one shopping list: unbought meal ingredients from every week, low pantry items,
    extras and regular items."""
    items = current_items(conn)
    stock = pantry_stock(conn, items, products)
    in_pantry = [
        {"key": item.key, "name": item.name, "qty": item.qty, "meals": item.meals,
         "product": stock[item.key]["product"], "auto": stock[item.key]["auto"]}
        for item in sorted(items.values(), key=lambda i: i.name.lower()) if item.key in stock
    ]
    items = {k: v for k, v in items.items() if k not in stock}
    overrides = dict(conn.execute("SELECT name, aisle FROM aisles").fetchall())
    qty_over = dict(conn.execute("SELECT item_key, qty FROM qty_overrides").fetchall())
    checked = {r[0] for r in conn.execute("SELECT item_key FROM checked WHERE week_start = ?", (LIST,))}

    groups: dict[str, list[dict]] = {a: [] for a in AISLES}

    def add(row: dict) -> None:
        if row["key"] in qty_over:
            row["qty"], row["qty_edited"] = qty_over[row["key"]], True
        groups.setdefault(row["aisle"], []).append(row)

    for item in items.values():
        aisle = overrides.get(item.key) or guess_aisle(item.name)
        add({"key": item.key, "name": item.name, "qty": item.qty, "meals": item.meals,
             "aisle": aisle, "checked": item.key in checked, "extra_id": None})
    hidden = dict(conn.execute("SELECT item_key, signature FROM removed WHERE week_start = ? AND item_key LIKE 'p:%'",
                               (LIST,)).fetchall())
    for prod in low_products(products):
        key = f"p:{prod['id']}"
        if hidden.get(key) == restock_signature(prod):
            continue
        aisle = overrides.get(merge_key(prod["name"])) or guess_aisle(prod["name"])
        add({"key": key, "name": prod["name"], "qty": str(prod["weekly_shop_qty"]), "meals": [],
             "aisle": aisle, "checked": key in checked, "extra_id": None,
             "pantry_low": {"product_id": prod["id"], "quantity": prod["quantity"]}})
    for r in conn.execute("SELECT id, name, qty, regular_id FROM extras WHERE week_start = ? ORDER BY id", (LIST,)):
        key = f"x:{r['id']}"
        row = {"key": key, "name": r["name"], "qty": r["qty"], "meals": [], "aisle": "Extras",
               "checked": key in checked, "extra_id": r["id"]}
        if r["regular_id"] is not None:   # Regular items go in their normal aisle
            row["aisle"] = overrides.get(merge_key(r["name"])) or guess_aisle(r["name"])
            row["regular"] = True
        groups.setdefault(row["aisle"], []).append(row)
    regulars = [
        dict(r) | {"on_list": bool(r["on_list"])}
        for r in conn.execute(
            """SELECT g.id, g.name, g.qty,
                      EXISTS(SELECT 1 FROM extras x WHERE x.regular_id = g.id AND x.week_start = ?) AS on_list
               FROM regulars g ORDER BY g.name COLLATE NOCASE""", (LIST,))
    ]

    aisles = []
    for name, rows in groups.items():
        if rows:
            if name != "Extras":
                rows.sort(key=lambda x: x["name"].lower())
            aisles.append({"name": name, "items": rows})
    total = sum(len(a["items"]) for a in aisles)
    left = sum(1 for a in aisles for i in a["items"] if not i["checked"])
    return {"aisles": aisles, "total": total, "left": left,
            "in_pantry": in_pantry, "pantry_ok": pantry_ok, "regulars": regulars}


def take_off_list(conn: sqlite3.Connection, key: str, items: dict[str, Item],
                  products: list[dict] | None) -> bool:
    """Bought, deleted or cleared: take one item off the list for good. Returns False if it isn't on it.

    Meal ingredients are marked bought for exactly the planned meals they came from, so planning
    another meal that needs them puts only the new amount on the list.
    """
    found = True
    if key.startswith("x:"):
        found = conn.execute("DELETE FROM extras WHERE id = ? AND week_start = ?", (key[2:], LIST)).rowcount > 0
    elif key.startswith("p:"):
        prod = next((p for p in low_products(products) if f"p:{p['id']}" == key), None)
        if prod is None:
            found = False
        else:
            conn.execute("INSERT INTO removed(week_start, item_key, signature) VALUES (?,?,?) "
                         "ON CONFLICT(week_start, item_key) DO UPDATE SET signature = excluded.signature",
                         (LIST, key, restock_signature(prod)))
    elif key in items:
        conn.executemany("INSERT OR IGNORE INTO bought(plan_id, item_key) VALUES (?, ?)",
                         [(pid, key) for pid in items[key].plan_ids])
    else:
        found = False
    conn.execute("DELETE FROM checked WHERE week_start = ? AND item_key = ?", (LIST, key))
    conn.execute("DELETE FROM qty_overrides WHERE item_key = ?", (key,))
    return found
