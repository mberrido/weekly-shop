import pytest

from app.photo_search import PhotoSearchError
from tests.conftest import PASSWORD

JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 100 + b"\xff\xd9"


class FakePexels:
    configured = True

    def __init__(self):
        self.searches, self.fail = [], False

    async def search(self, query, per_page=15):
        if self.fail:
            raise PhotoSearchError("Couldn't reach Pexels while searching.")
        self.searches.append(query)
        if "nothing" in query.lower():
            return []
        return [{"id": 100 + i, "thumb": f"https://images.pexels.com/{i}.jpg", "alt": query, "photographer": f"P{i}"}
                for i in range(per_page)]

    async def download(self, photo_id):
        if self.fail:
            raise PhotoSearchError("Couldn't reach Pexels while downloading the photo.")
        return JPEG + str(photo_id).encode(), f"P{photo_id} / Pexels"


@pytest.fixture
def pexels():
    return FakePexels()


@pytest.fixture
def pc(make_client, pexels):
    c = make_client(photos=pexels)
    c.post("/login", data={"password": PASSWORD})
    return c


def new_meal(c, name="Porridge", **params):
    q = "?auto_photo=false" if params.get("auto_photo") is False else ""
    return c.post(f"/api/meals{q}", json={"name": name, "kind": "breakfast", "servings": 1}).json()


def meal(c, meal_id):
    return c.get(f"/api/meals/{meal_id}").json()


def test_new_meal_gets_a_photo_automatically(pc, pexels):
    m = new_meal(pc)
    got = meal(pc, m["id"])
    assert got["photo_url"] and got["photo_credit"] == "P100 / Pexels"
    assert pc.get(got["photo_url"]).content == JPEG + b"100"
    assert pexels.searches == ["Porridge"]


def test_no_auto_photo_when_client_uploads_its_own(pc, pexels):
    m = new_meal(pc, auto_photo=False)
    assert meal(pc, m["id"])["photo_url"] is None and pexels.searches == []


def test_no_results_leaves_it_blank(pc):
    m = new_meal(pc, "Nothing matches this")
    assert meal(pc, m["id"])["photo_url"] is None


def test_pexels_down_does_not_break_saving(pc, pexels):
    pexels.fail = True
    r = pc.post("/api/meals", json={"name": "Porridge", "servings": 1})
    assert r.status_code == 201 and meal(pc, r.json()["id"])["photo_url"] is None
    s = pc.get("/api/photo-search?q=porridge")
    assert s.status_code == 502 and "Pexels" in s.json()["detail"]


def test_search_and_pick(pc):
    m = new_meal(pc, auto_photo=False)
    hits = pc.get("/api/photo-search?q=porridge").json()
    assert len(hits) == 15 and hits[3]["id"] == 103
    r = pc.post(f"/api/meals/{m['id']}/photo/pexels", json={"photo_id": 103})
    assert r.status_code == 200 and r.json()["photo_credit"] == "P103 / Pexels"
    assert pc.post("/api/meals/999/photo/pexels", json={"photo_id": 1}).status_code == 404


def test_upload_clears_credit(pc):
    m = new_meal(pc)
    r = pc.put(f"/api/meals/{m['id']}/photo", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert r.json()["photo_credit"] is None


def test_removed_photo_is_not_refilled(pc):
    m = new_meal(pc)
    pc.delete(f"/api/meals/{m['id']}/photo")
    assert pc.loop.run_until_complete(pc.app.state.autofill_missing_photos()) == 0
    assert meal(pc, m["id"])["photo_url"] is None


def test_backfill_existing_meals(pc, pexels):
    a = new_meal(pc, "Porridge", auto_photo=False)
    b = new_meal(pc, "Tuna melt", auto_photo=False)
    pc.post("/api/cookidoo/import", json={"ref": "r12345"})   # has a Cookidoo image, so skipped
    pexels.searches.clear()
    added = pc.loop.run_until_complete(pc.app.state.autofill_missing_photos())
    assert added == 2 and pexels.searches == ["Porridge", "Tuna melt"]
    assert meal(pc, a["id"])["photo_url"] and meal(pc, b["id"])["photo_url"]


def test_not_configured(client):
    assert client.get("/api/config").json()["photos"] is False
    assert client.get("/api/photo-search?q=porridge").status_code == 502
    m = client.post("/api/meals", json={"name": "Porridge", "servings": 1}).json()
    assert m["photo_url"] is None
