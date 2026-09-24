"""SQLite schema and connection helpers (plain sqlite3, no ORM)."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'dinner'
                CHECK (kind IN ('breakfast','lunch','dinner','any')),
    source      TEXT NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual','cookidoo','cookidoo_custom')),
    cookidoo_id TEXT UNIQUE,
    servings    INTEGER NOT NULL DEFAULT 4 CHECK (servings > 0),
    url         TEXT,
    image       TEXT,          -- Cookidoo image URL
    photo       TEXT,          -- photo file name in <data>/photos (uploaded or from Pexels)
    photo_credit TEXT,         -- e.g. 'Jane Doe / Pexels'
    no_auto_photo INTEGER NOT NULL DEFAULT 0,  -- set when the user removes a photo
    kinds       TEXT NOT NULL DEFAULT ''       -- meal times, e.g. 'lunch,dinner' (supersedes kind)
);

CREATE TABLE IF NOT EXISTS ingredients (
    id       INTEGER PRIMARY KEY,
    meal_id  INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    name     TEXT NOT NULL,
    qty      REAL,
    unit     TEXT NOT NULL DEFAULT '',
    note     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ingredients_meal ON ingredients(meal_id, position);

CREATE TABLE IF NOT EXISTS plan (
    id         INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    day        INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
    slot       TEXT NOT NULL CHECK (slot IN ('breakfast','lunch','dinner')),
    meal_id    INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    servings   INTEGER CHECK (servings IS NULL OR servings > 0)
);
CREATE INDEX IF NOT EXISTS plan_week ON plan(week_start);

CREATE TABLE IF NOT EXISTS extras (
    id         INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    name       TEXT NOT NULL,
    qty        TEXT NOT NULL DEFAULT '',
    regular_id INTEGER             -- set when added from a Regular item
);

-- Meal ingredients already bought (or cleared) for a planned meal, so they stay off the list.
CREATE TABLE IF NOT EXISTS bought (
    plan_id  INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    PRIMARY KEY (plan_id, item_key)
);

-- Quantities typed over the calculated ones on the shopping list.
CREATE TABLE IF NOT EXISTS qty_overrides (
    item_key TEXT PRIMARY KEY,
    qty      TEXT NOT NULL
);

-- Things bought often; ticking one adds it to the list as an extra.
CREATE TABLE IF NOT EXISTS regulars (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    qty  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS extras_week ON extras(week_start);

CREATE TABLE IF NOT EXISTS checked (
    week_start TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    PRIMARY KEY (week_start, item_key)
);

CREATE TABLE IF NOT EXISTS pantry (
    name TEXT PRIMARY KEY          -- stored as parsing.merge_key(name)
);

-- Items deleted from a week's list. `signature` records what the item was made
-- of (plan entries + quantity); if that changes, e.g. the item is needed again
-- by another meal, it reappears.
CREATE TABLE IF NOT EXISTS removed (
    week_start TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    signature  TEXT NOT NULL,
    PRIMARY KEY (week_start, item_key)
);

CREATE TABLE IF NOT EXISTS aisles (
    name  TEXT PRIMARY KEY,        -- stored as parsing.merge_key(name)
    aisle TEXT NOT NULL
);

-- Manual Pantry Tracker links; product_id NULL means "never match this item".
-- Items without a row are auto-matched by name.
CREATE TABLE IF NOT EXISTS pantry_links (
    name       TEXT PRIMARY KEY,   -- stored as parsing.merge_key(name)
    product_id INTEGER
);
"""

PANTRY_SEED = ["salt", "water", "black pepper", "ground black pepper", "ice cubes"]
SCHEMA_VERSION = 6


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(path: str) -> Iterator[sqlite3.Connection]:
    """A connection that commits on success and always closes."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str) -> None:
    from .parsing import merge_key

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with session(path) as conn:
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            conn.executemany(
                "INSERT OR IGNORE INTO pantry(name) VALUES (?)",
                [(merge_key(n),) for n in PANTRY_SEED],
            )
        if version < 2:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(meals)")}
            if "photo" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN photo TEXT")
        if version < 3:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(meals)")}
            if "photo_credit" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN photo_credit TEXT")
            if "no_auto_photo" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN no_auto_photo INTEGER NOT NULL DEFAULT 0")
        if version < 4:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(meals)")}
            if "kinds" not in cols:
                conn.execute("ALTER TABLE meals ADD COLUMN kinds TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE meals SET kinds = CASE kind WHEN 'any' THEN 'breakfast,lunch,dinner' ELSE kind END "
                         "WHERE kinds = ''")
        if version < 5:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(extras)")}
            if "regular_id" not in cols:
                conn.execute("ALTER TABLE extras ADD COLUMN regular_id INTEGER")
        if version < 6:
            _to_one_list(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _to_one_list(conn: sqlite3.Connection) -> None:
    """v6: per-week shopping lists become one list.

    Meals from past weeks count as bought (they're history). Ticks, extras, deleted items and
    hidden pantry lines from this week onwards carry over; older weeks' list state is dropped.
    """
    from .parsing import merge_key
    from .shopping import LIST, merge_items, monday_of, pantry_keys, planned_meals

    this_week = monday_of().isoformat()
    pantry = pantry_keys(conn)
    rows = conn.execute("SELECT DISTINCT week_start FROM plan").fetchall()
    for (ws,) in rows:
        planned = planned_meals(conn, ws)
        if ws < this_week:
            done = [(pm.plan_id, merge_key(i["name"])) for pm in planned for i in pm.ingredients if merge_key(i["name"])]
        else:
            # Items deleted from / emptied out of this week's list were bought.
            gone = {r[0] for r in conn.execute(
                "SELECT item_key FROM removed WHERE week_start = ? AND item_key NOT LIKE 'p:%'", (ws,))}
            items = merge_items(planned, pantry)
            done = [(pid, key) for key in gone if key in items for pid in items[key].plan_ids]
        conn.executemany("INSERT OR IGNORE INTO bought(plan_id, item_key) VALUES (?, ?)", done)
    conn.execute("DELETE FROM extras WHERE week_start < ? AND week_start != ?", (this_week, LIST))
    conn.execute("UPDATE extras SET week_start = ?", (LIST,))
    for table in ("checked", "removed"):
        conn.execute(f"DELETE FROM {table} WHERE week_start < ? AND week_start != ?", (this_week, LIST))
        conn.execute(f"UPDATE OR REPLACE {table} SET week_start = ?", (LIST,))
    conn.execute("DELETE FROM removed WHERE item_key NOT LIKE 'p:%'")   # meal items now tracked in `bought`


def backup(path: str, keep: int = 14) -> Path | None:
    """Write a consistent daily snapshot to <data>/backups using SQLite's backup API.

    Hyper Backup then copies a file that is never mid-write.
    """
    src_path = Path(path)
    if not src_path.exists():
        return None
    dest_dir = src_path.parent / "backups"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{src_path.stem}-{date.today().isoformat()}.db"
    src = connect(path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    snapshots = sorted(dest_dir.glob(f"{src_path.stem}-*.db"))
    for old in snapshots[:-keep]:
        old.unlink(missing_ok=True)
    log.info("Database snapshot written to %s at %s", dest.name, datetime.now().isoformat(timespec="seconds"))
    return dest
