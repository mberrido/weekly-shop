import sqlite3

from app.db import init_db

WEEK = "2026-09-21"


def lst(client, week=WEEK):
    return client.get(f"/api/list?week={week}").json()


def on_list(client, name, week=WEEK):
    return next((i for a in lst(client, week)["aisles"] for i in a["items"] if i["name"] == name), None)


def regular(client, name, week=WEEK):
    return next(r for r in lst(client, week)["regulars"] if r["name"] == name)


def test_full_flow(client):
    rid = client.post("/api/regulars", json={"name": "Bread", "qty": "2 loaves"}).json()["id"]
    client.post("/api/regulars", json={"name": "Bananas"})
    assert [r["name"] for r in lst(client)["regulars"]] == ["Bananas", "Bread"]
    assert not regular(client, "Bread")["on_list"]

    # Tick it in Regular items -> on the list, in its own aisle.
    assert client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK}).json() == {"on_list": True}
    item = on_list(client, "Bread")
    assert item["aisle"] == "Bakery" and item["qty"] == "2 loaves" and item["regular"] and not item["checked"]
    assert regular(client, "Bread")["on_list"]

    # Tick it on the list -> basket; empty basket -> cleared, and the regular is unticked again.
    client.post("/api/list/check", json={"week_start": WEEK, "key": item["key"], "checked": True})
    assert client.post("/api/list/empty-basket", json={"week_start": WEEK}).json() == {"emptied": 1}
    assert on_list(client, "Bread") is None
    assert not regular(client, "Bread")["on_list"]


def test_untick_takes_it_off_again(client):
    rid = client.post("/api/regulars", json={"name": "Kitchen roll"}).json()["id"]
    client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK})
    assert client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK}).json() == {"on_list": False}
    assert on_list(client, "Kitchen roll") is None


def test_one_list_whatever_week_and_delete_from_list(client):
    rid = client.post("/api/regulars", json={"name": "Milk"}).json()["id"]
    client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK})
    assert regular(client, "Milk", "2026-09-28")["on_list"]            # there's only one list
    item = on_list(client, "Milk")
    client.post("/api/list/remove", json={"key": item["key"]})
    assert not regular(client, "Milk")["on_list"]


def test_aisle_override_applies(client):
    rid = client.post("/api/regulars", json={"name": "Bread"}).json()["id"]
    client.put("/api/aisles", json={"name": "Bread", "aisle": "Frozen"})
    client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK})
    assert on_list(client, "Bread")["aisle"] == "Frozen"


def test_deleting_a_regular(client):
    rid = client.post("/api/regulars", json={"name": "Bread"}).json()["id"]
    assert client.delete(f"/api/regulars/{rid}").status_code == 204
    assert lst(client)["regulars"] == []
    assert client.post(f"/api/regulars/{rid}/toggle", json={"week_start": WEEK}).status_code == 404


def test_validation(client):
    assert client.post("/api/regulars", json={"name": ""}).status_code == 422


def test_plain_extras_still_go_in_extras(client):
    client.post("/api/extras", json={"week_start": WEEK, "name": "Bread"})
    assert on_list(client, "Bread")["aisle"] == "Extras"


def test_migration_adds_regular_id(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE extras (id INTEGER PRIMARY KEY, week_start TEXT NOT NULL, name TEXT NOT NULL, "
                       "qty TEXT NOT NULL DEFAULT ''); INSERT INTO extras(week_start, name) VALUES ('2026-09-21', 'Bin bags');"
                       "PRAGMA user_version = 4;")
    conn.close()
    init_db(str(path))
    conn = sqlite3.connect(path)
    assert "regular_id" in {r[1] for r in conn.execute("PRAGMA table_info(extras)")}
    assert conn.execute("SELECT name FROM extras").fetchone()[0] == "Bin bags"
