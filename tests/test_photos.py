import sqlite3

from app.db import init_db

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
H = {"Content-Type": "image/jpeg"}


def make_meal(client):
    return client.post("/api/meals", json={"name": "Porridge", "kind": "breakfast", "servings": 1}).json()


def test_upload_serve_replace_and_remove(client, settings):
    meal = make_meal(client)
    assert meal["photo_url"] is None
    r = client.put(f"/api/meals/{meal['id']}/photo", content=JPEG, headers=H)
    assert r.status_code == 200, r.text
    url = r.json()["photo_url"]
    assert url.startswith(f"/photos/{meal['id']}-") and url.endswith(".jpg")
    got = client.get(url)
    assert got.status_code == 200 and got.content == JPEG and got.headers["content-type"] == "image/jpeg"
    assert client.get("/api/meals").json()[0]["photo_url"] == url

    photos = settings.data_dir / "photos"
    url2 = client.put(f"/api/meals/{meal['id']}/photo", content=JPEG, headers=H).json()["photo_url"]
    assert url2 != url and client.get(url).status_code == 404      # old file cleaned up
    assert len(list(photos.glob("*.jpg"))) == 1

    assert client.delete(f"/api/meals/{meal['id']}/photo").json()["photo_url"] is None
    assert list(photos.glob("*.jpg")) == []


def test_deleting_meal_removes_photo(client, settings):
    meal = make_meal(client)
    client.put(f"/api/meals/{meal['id']}/photo", content=JPEG, headers=H)
    client.delete(f"/api/meals/{meal['id']}")
    assert list((settings.data_dir / "photos").glob("*.jpg")) == []


def test_rejects_bad_uploads(client):
    meal = make_meal(client)
    assert client.put(f"/api/meals/{meal['id']}/photo", content=b"<svg/>", headers={"Content-Type": "image/svg+xml"}).status_code == 415
    assert client.put(f"/api/meals/{meal['id']}/photo", content=b"not a jpeg", headers=H).status_code == 400
    big = b"\xff\xd8\xff" + b"\x00" * (8 * 1024 * 1024 + 10)
    assert client.put(f"/api/meals/{meal['id']}/photo", content=big, headers=H).status_code == 413
    assert client.put("/api/meals/999/photo", content=JPEG, headers=H).status_code == 404


def test_photo_names_are_validated(client):
    for bad in ["../shop.db", "..%2Fshop.db", "x.jpg", "1-zzzz.jpg"]:
        assert client.get(f"/photos/{bad}").status_code == 404


def test_photos_need_login(client, anon):
    meal = make_meal(client)
    url = client.put(f"/api/meals/{meal['id']}/photo", content=JPEG, headers=H).json()["photo_url"]
    r = anon.get(url)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_migration_adds_photo_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)   # a v1 database without the column
    conn.executescript("CREATE TABLE meals (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'dinner', "
                       "source TEXT NOT NULL DEFAULT 'manual', cookidoo_id TEXT UNIQUE, servings INTEGER NOT NULL DEFAULT 4, "
                       "url TEXT, image TEXT); INSERT INTO meals(name) VALUES ('Old'); PRAGMA user_version = 1;")
    conn.close()
    init_db(str(path))
    conn = sqlite3.connect(path)
    assert "photo" in {r[1] for r in conn.execute("PRAGMA table_info(meals)")}
    assert conn.execute("SELECT name FROM meals").fetchone()[0] == "Old"
