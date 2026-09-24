"""One shopping list across all weeks; quantity changes; Clear list; Clear week."""
import sqlite3

from app.db import init_db
from app.shopping import monday_of

W1, W2 = "2026-09-21", "2026-09-28"


def meal(client, name, ingredients, servings=4):
    return client.post("/api/meals", json={"name": name, "servings": servings, "ingredients": ingredients}).json()


def plan(client, meal_id, week, day=0):
    return client.post("/api/plan", json={"week_start": week, "day": day, "slot": "dinner", "meal_id": meal_id}).json()["id"]


def items(client):
    lst = client.get("/api/list").json()
    return {i["key"]: i for a in lst["aisles"] for i in a["items"]}


def test_list_covers_every_week(client):
    stew = meal(client, "Stew", [{"name": "carrots", "qty": 3}, {"name": "beef", "qty": 400, "unit": "g"}])
    curry = meal(client, "Curry", [{"name": "carrots", "qty": 2}, {"name": "rice", "qty": 300, "unit": "g"}])
    plan(client, stew["id"], W1)
    plan(client, curry["id"], W2)
    got = items(client)
    assert got["carrot"]["qty"] == "5" and got["carrot"]["meals"] == ["Stew", "Curry"]
    assert {"beef", "rice"} <= set(got)
    # The week passed in doesn't matter any more.
    assert client.get(f"/api/list?week={W2}").json() == client.get("/api/list").json()


def test_buying_then_planning_more_adds_only_the_new_amount(client):
    stew = meal(client, "Stew", [{"name": "carrots", "qty": 3}])
    plan(client, stew["id"], W1)
    client.post("/api/list/check", json={"key": "carrot", "checked": True})
    client.post("/api/list/empty-basket")
    assert "carrot" not in items(client)
    plan(client, stew["id"], W2)
    assert items(client)["carrot"]["qty"] == "3"


def test_change_quantity(client):
    stew = meal(client, "Stew", [{"name": "beef", "qty": 750, "unit": "g"}])
    plan(client, stew["id"], W1)
    assert client.put("/api/list/qty", json={"key": "beef", "qty": "1 kg"}).status_code == 200
    assert items(client)["beef"]["qty"] == "1 kg" and items(client)["beef"]["qty_edited"]
    client.put("/api/list/qty", json={"key": "beef", "qty": ""})           # back to calculated
    assert items(client)["beef"]["qty"] == "750 g" and "qty_edited" not in items(client)["beef"]
    # Extras store the new amount directly.
    xid = client.post("/api/extras", json={"name": "Bin bags", "qty": "1"}).json()["id"]
    client.put("/api/list/qty", json={"key": f"x:{xid}", "qty": "2 rolls"})
    assert items(client)[f"x:{xid}"]["qty"] == "2 rolls"


def test_override_cleared_once_bought(client):
    stew = meal(client, "Stew", [{"name": "beef", "qty": 750, "unit": "g"}])
    plan(client, stew["id"], W1)
    client.put("/api/list/qty", json={"key": "beef", "qty": "1 kg"})
    client.post("/api/list/remove", json={"key": "beef"})
    plan(client, stew["id"], W2)
    assert items(client)["beef"]["qty"] == "750 g"


def test_clear_list(client):
    stew = meal(client, "Stew", [{"name": "carrots", "qty": 3}, {"name": "beef", "qty": 400, "unit": "g"}])
    plan(client, stew["id"], W1)
    client.post("/api/extras", json={"name": "Bin bags"})
    rid = client.post("/api/regulars", json={"name": "Bread"}).json()["id"]
    client.post(f"/api/regulars/{rid}/toggle")
    client.post("/api/list/check", json={"key": "beef", "checked": True})
    assert client.post("/api/list/clear").json() == {"cleared": 4}
    lst = client.get("/api/list").json()
    assert lst["total"] == 0 and not lst["regulars"][0]["on_list"]
    assert len(client.get(f"/api/week?start={W1}").json()["entries"]) == 1    # meals themselves stay planned
    plan(client, stew["id"], W2)                                              # new plans come through
    assert set(items(client)) == {"carrot", "beef"}


def test_clear_week(client):
    stew = meal(client, "Stew", [{"name": "carrots", "qty": 3}])
    curry = meal(client, "Curry", [{"name": "rice", "qty": 300, "unit": "g"}])
    plan(client, stew["id"], W1)
    plan(client, stew["id"], W1, day=2)
    plan(client, curry["id"], W2)
    assert client.post("/api/week/clear", json={"week_start": "2026-09-24"}).json() == {"cleared": 2}   # any day in the week
    assert client.get(f"/api/week?start={W1}").json()["entries"] == []
    assert set(items(client)) == {"rice"}                                    # stew's carrots left the list


def test_migration_to_one_list(tmp_path):
    """Past weeks count as bought; this week's ticks, extras and deletions carry over."""
    this_week = monday_of().isoformat()
    path = tmp_path / "v5.db"
    init_db(str(path))
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        INSERT INTO meals(id, name, kinds) VALUES (1, 'Stew', 'dinner');
        INSERT INTO ingredients(meal_id, name, qty) VALUES (1, 'carrots', 3), (1, 'beef', 400);
        INSERT INTO plan(id, week_start, day, slot, meal_id) VALUES (1, '2020-01-06', 0, 'dinner', 1), (2, '{this_week}', 0, 'dinner', 1);
        INSERT INTO extras(week_start, name) VALUES ('2020-01-06', 'Old extra'), ('{this_week}', 'Bin bags');
        INSERT INTO checked(week_start, item_key) VALUES ('{this_week}', 'beef'), ('2020-01-06', 'carrot');
        INSERT INTO removed(week_start, item_key, signature) VALUES ('{this_week}', 'carrot', 'x');
        PRAGMA user_version = 5;
    """)
    conn.close()
    init_db(str(path))
    conn = sqlite3.connect(path)
    assert set(conn.execute("SELECT plan_id, item_key FROM bought")) == {(1, "carrot"), (1, "beef"), (2, "carrot")}
    assert conn.execute("SELECT week_start, name FROM extras").fetchall() == [("list", "Bin bags")]
    assert conn.execute("SELECT week_start, item_key FROM checked").fetchall() == [("list", "beef")]
    assert conn.execute("SELECT count(*) FROM removed").fetchone()[0] == 0
