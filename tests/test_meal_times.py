import sqlite3

from app.db import init_db

WEEK = "2026-09-21"


def test_multiple_meal_times(client):
    m = client.post("/api/meals", json={"name": "Wraps", "kinds": ["dinner", "lunch"], "servings": 2}).json()
    assert m["kinds"] == ["lunch", "dinner"]                      # stored in a fixed order
    r = client.put(f"/api/meals/{m['id']}", json={"name": "Wraps", "kinds": ["breakfast"], "servings": 2})
    assert r.json()["kinds"] == ["breakfast"]


def test_needs_at_least_one_meal_time(client):
    assert client.post("/api/meals", json={"name": "X", "kinds": []}).status_code == 422
    assert client.post("/api/meals", json={"name": "X", "kinds": ["brunch"]}).status_code == 422


def test_old_single_kind_still_accepted(client):
    assert client.post("/api/meals", json={"name": "A", "kind": "any"}).json()["kinds"] == ["breakfast", "lunch", "dinner"]
    assert client.post("/api/meals", json={"name": "B", "kind": "lunch"}).json()["kinds"] == ["lunch"]
    assert client.post("/api/meals", json={"name": "C"}).json()["kinds"] == ["breakfast", "lunch", "dinner"]


def test_import_with_meal_times(client):
    r = client.post("/api/cookidoo/import", json={"ref": "r12345", "kinds": ["lunch", "dinner"]})
    assert r.json()["meal"]["kinds"] == ["lunch", "dinner"]


def test_migration_converts_old_kinds(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meals (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'dinner', "
        "source TEXT NOT NULL DEFAULT 'manual', cookidoo_id TEXT UNIQUE, servings INTEGER NOT NULL DEFAULT 4, "
        "url TEXT, image TEXT, photo TEXT, photo_credit TEXT, no_auto_photo INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO meals(name, kind) VALUES ('Porridge', 'breakfast'), ('Soup', 'any'); PRAGMA user_version = 3;")
    conn.close()
    init_db(str(path))
    rows = dict(sqlite3.connect(path).execute("SELECT name, kinds FROM meals").fetchall())
    assert rows == {"Porridge": "breakfast", "Soup": "breakfast,lunch,dinner"}


# -- empty basket -----------------------------------------------------------------

def _items(client):
    lst = client.get(f"/api/list?week={WEEK}").json()
    return {i["key"]: i for a in lst["aisles"] for i in a["items"]}


def test_empty_basket(client):
    meal = client.post("/api/meals", json={"name": "Stew", "servings": 4, "ingredients": [
        {"name": "carrots", "qty": 3}, {"name": "beef", "qty": 400, "unit": "g"}, {"name": "potatoes", "qty": 1, "unit": "kg"}]}).json()
    client.post("/api/plan", json={"week_start": WEEK, "day": 0, "slot": "dinner", "meal_id": meal["id"]})
    xid = client.post("/api/extras", json={"week_start": WEEK, "name": "Bin bags"}).json()["id"]
    for key in ["carrot", "beef", f"x:{xid}"]:
        client.post("/api/list/check", json={"week_start": WEEK, "key": key, "checked": True})
    assert client.post("/api/list/empty-basket", json={"week_start": WEEK}).json() == {"emptied": 3}
    items = _items(client)
    assert list(items) == ["potato"] and not items["potato"]["checked"]
    # Emptied items come back if they're needed again.
    client.post("/api/plan", json={"week_start": WEEK, "day": 1, "slot": "dinner", "meal_id": meal["id"]})
    assert {"carrot", "beef", "potato"} <= set(_items(client))


def test_empty_basket_when_nothing_ticked(client):
    assert client.post("/api/list/empty-basket", json={"week_start": WEEK}).json() == {"emptied": 0}
