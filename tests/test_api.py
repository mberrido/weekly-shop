from cookidoo_api import CookidooRequestException

from app.cookidoo_service import CookidooService
from tests.conftest import PASSWORD

WEEK = "2026-09-21"


def make_meal(client, name="Curry", servings=4, kind="dinner", ingredients=None):
    ingredients = ingredients if ingredients is not None else [
        {"name": "chicken thighs", "qty": 500, "unit": "g"},
        {"name": "red onions", "qty": 2},
        {"name": "salt", "qty": 1, "unit": "pinch"},
        {"name": "coconut milk", "qty": 400, "unit": "ml"},
        {"name": "wraps"},
    ]
    r = client.post("/api/meals", json={"name": name, "kind": kind, "servings": servings, "ingredients": ingredients})
    assert r.status_code == 201, r.text
    return r.json()


def plan(client, meal_id, day=0, slot="dinner", servings=None, week=WEEK):
    r = client.post("/api/plan", json={"week_start": week, "day": day, "slot": slot,
                                       "meal_id": meal_id, "servings": servings})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def items_by_name(lst):
    return {i["name"]: i for a in lst["aisles"] for i in a["items"]}


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "version": "dev"}


def test_meal_crud(client):
    meal = make_meal(client)
    assert meal["ingredients"][0] == {"name": "chicken thighs", "qty": 500, "unit": "g", "note": ""}
    r = client.put(f"/api/meals/{meal['id']}", json={"name": "Thai curry", "kind": "dinner", "servings": 2,
                                                     "ingredients": [{"name": "rice", "qty": 300, "unit": "g"}]})
    assert r.json()["name"] == "Thai curry" and len(r.json()["ingredients"]) == 1
    assert [m["name"] for m in client.get("/api/meals").json()] == ["Thai curry"]
    assert client.delete(f"/api/meals/{meal['id']}").status_code == 204
    assert client.get(f"/api/meals/{meal['id']}").status_code == 404


def test_meal_validation(client):
    assert client.post("/api/meals", json={"name": "", "servings": 4}).status_code == 422
    assert client.post("/api/meals", json={"name": "x", "kind": "brunch"}).status_code == 422


def test_week_plan_and_servings(client):
    meal = make_meal(client)
    pid = plan(client, meal["id"], day=2)
    week = client.get("/api/week?start=2026-09-24").json()  # any day normalises to Monday
    assert week["week_start"] == WEEK and week["days"][6] == "2026-09-27"
    assert week["entries"][0]["servings"] == 4 and week["entries"][0]["day"] == 2
    assert client.patch(f"/api/plan/{pid}", json={"servings": 6}).status_code == 200
    assert client.get(f"/api/week?start={WEEK}").json()["entries"][0]["servings"] == 6
    assert client.post("/api/plan", json={"week_start": WEEK, "day": 7, "slot": "dinner", "meal_id": meal["id"]}).status_code == 422
    assert client.post("/api/plan", json={"week_start": WEEK, "day": 0, "slot": "dinner", "meal_id": 999}).status_code == 404


def test_list_scaling_merging_pantry_and_aisles(client):
    curry = make_meal(client)
    wraps = make_meal(client, "Wraps", servings=2, kind="lunch",
                      ingredients=[{"name": "Red onion", "qty": 1}, {"name": "wraps", "qty": 4},
                                   {"name": "peanut butter", "qty": 2, "unit": "tbsp"}])
    plan(client, curry["id"], day=0, servings=6)   # x1.5
    plan(client, wraps["id"], day=1, slot="lunch")  # x1
    lst = client.get(f"/api/list?week={WEEK}").json()
    items = items_by_name(lst)
    assert items["chicken thighs"]["qty"] == "750 g"
    assert items["red onions"]["qty"] == "4"            # 2*1.5 + 1
    assert items["red onions"]["meals"] == ["Curry", "Wraps"]
    assert items["coconut milk"]["aisle"] == "Cupboard"
    assert items["coconut milk"]["qty"] == "600 ml"
    assert items["peanut butter"]["aisle"] == "Cupboard"
    assert items["wraps"]["qty"] == "4 + some"
    assert "salt" not in items                           # seeded pantry
    assert [a["name"] for a in lst["aisles"]] == ["Fruit & veg", "Meat & fish", "Bakery", "Cupboard"]
    assert lst["total"] == lst["left"] == 5


def test_check_untick_extras_pantry_aisle_override(client):
    meal = make_meal(client)
    plan(client, meal["id"])
    assert client.post("/api/list/check", json={"week_start": WEEK, "key": "chicken thigh", "checked": True}).status_code == 200
    lst = client.get(f"/api/list?week={WEEK}").json()
    assert items_by_name(lst)["chicken thighs"]["checked"] and lst["left"] == lst["total"] - 1

    r = client.post("/api/extras", json={"week_start": WEEK, "name": "Bin bags", "qty": "1 roll"})
    extra_id = r.json()["id"]
    lst = client.get(f"/api/list?week={WEEK}").json()
    assert lst["aisles"][-1]["name"] == "Extras"
    assert lst["aisles"][-1]["items"][0] == {"key": f"x:{extra_id}", "name": "Bin bags", "qty": "1 roll", "meals": [],
                                             "aisle": "Extras", "checked": False, "extra_id": extra_id}
    client.post("/api/list/untick-all", json={"week_start": WEEK})
    assert client.get(f"/api/list?week={WEEK}").json()["left"] == lst["total"]

    client.put("/api/aisles", json={"name": "Coconut milk", "aisle": "Other"})
    assert items_by_name(client.get(f"/api/list?week={WEEK}").json())["coconut milk"]["aisle"] == "Other"
    assert client.put("/api/aisles", json={"name": "x", "aisle": "Garden"}).status_code == 400
    client.put("/api/aisles", json={"name": "coconut milk", "aisle": None})
    assert items_by_name(client.get(f"/api/list?week={WEEK}").json())["coconut milk"]["aisle"] == "Cupboard"

    client.post("/api/pantry", json={"name": "Wraps"})
    assert "wrap" in client.get("/api/pantry").json()
    assert "wraps" not in items_by_name(client.get(f"/api/list?week={WEEK}").json())
    client.delete("/api/pantry/wraps")
    assert "wraps" in items_by_name(client.get(f"/api/list?week={WEEK}").json())

    client.delete(f"/api/extras/{extra_id}")
    assert client.get(f"/api/list?week={WEEK}").json()["aisles"][-1]["name"] != "Extras"


def test_copy_last_week_skips_duplicates(client):
    meal = make_meal(client)
    plan(client, meal["id"], day=3, week="2026-09-14")
    plan(client, meal["id"], day=4, week="2026-09-14", slot="lunch", servings=2)
    assert client.post("/api/week/copy-last", json={"week_start": WEEK}).json() == {"copied": 2}
    assert client.post("/api/week/copy-last", json={"week_start": WEEK}).json() == {"copied": 0}
    entries = client.get(f"/api/week?start={WEEK}").json()["entries"]
    assert {(e["day"], e["slot"], e["servings"]) for e in entries} == {(3, "dinner", 4), (4, "lunch", 2)}


def test_delete_meal_cascades_to_plan(client):
    meal = make_meal(client)
    plan(client, meal["id"])
    client.delete(f"/api/meals/{meal['id']}")
    assert client.get(f"/api/week?start={WEEK}").json()["entries"] == []


# -- Cookidoo (mocked) ---------------------------------------------------------

def test_config_reports_cookidoo(client, make_client):
    assert client.get("/api/config").json()["cookidoo"] is True
    off = make_client(cookidoo=CookidooService("", ""))
    off.post("/login", data={"password": PASSWORD})
    assert off.get("/api/config").json()["cookidoo"] is False
    r = off.get("/api/cookidoo/search?q=soup")
    assert r.status_code == 503 and "isn't configured" in r.json()["detail"]
    assert off.get("/healthz").status_code == 200   # rest of app unaffected


def test_import_by_url_and_id(client, fake_api):
    r = client.post("/api/cookidoo/import", json={"ref": "https://cookidoo.co.uk/recipes/recipe/en-GB/r59322", "kind": "dinner"})
    assert r.status_code == 200, r.text
    meal = r.json()["meal"]
    assert r.json()["created"] and meal["source"] == "cookidoo" and meal["cookidoo_id"] == "r59322"
    assert meal["image"] == "https://img/thumb.jpg"
    assert meal["ingredients"][0] == {"name": "chicken thighs", "qty": 500, "unit": "g", "note": "diced"}
    assert meal["ingredients"][1]["qty"] == 2                       # "1 - 2" -> upper bound
    again = client.post("/api/cookidoo/import", json={"ref": "r59322", "kind": "lunch"}).json()
    assert again["created"] is False and again["meal"]["id"] == meal["id"]
    assert fake_api.calls.count(("details", "r59322")) == 1


def test_import_custom_recipe(client):
    r = client.post("/api/cookidoo/import", json={"ref": "https://cookidoo.co.uk/created-recipes/en-GB/01JABCDEFGHIJK", "kind": "any"})
    meal = r.json()["meal"]
    assert meal["source"] == "cookidoo_custom" and meal["servings"] == 8
    assert [(i["name"], i["qty"], i["unit"]) for i in meal["ingredients"]] == [
        ("oats", 200, "g"), ("butter", 100, "g"), ("golden syrup", None, "")]


def test_import_bad_ref(client):
    assert client.post("/api/cookidoo/import", json={"ref": "not a recipe"}).status_code == 400


def test_search(client):
    assert client.get("/api/cookidoo/search?q=soup").json()[0]["name"] == "soup pie"


def test_cookidoo_failure_returns_502_and_app_keeps_working(client, fake_api, caplog):
    fake_api.fail = CookidooRequestException("boom")
    r = client.get("/api/cookidoo/search?q=soup")
    assert r.status_code == 502 and "Couldn't reach Cookidoo" in r.json()["detail"]
    assert client.get(f"/api/list?week={WEEK}").status_code == 200
    assert "CookidooRequestException" in caplog.text


def test_pull_my_week(client):
    r = client.post("/api/cookidoo/pull-week", json={"week_start": WEEK})
    assert r.json() == {"added": 3, "imported": 3, "skipped": 0}
    entries = client.get(f"/api/week?start={WEEK}").json()["entries"]
    assert sorted((e["day"], e["slot"]) for e in entries) == [(0, "dinner"), (2, "dinner"), (2, "dinner")]
    assert client.post("/api/cookidoo/pull-week", json={"week_start": WEEK}).json() == {"added": 0, "imported": 0, "skipped": 3}


def test_send_list(client, fake_api):
    client.post("/api/cookidoo/import", json={"ref": "r10"})
    cookidoo_meal = client.get("/api/meals").json()[0]
    manual = make_meal(client, "Toast", ingredients=[{"name": "bread", "qty": 1}, {"name": "butter", "qty": 50, "unit": "g"},
                                                     {"name": "salt"}])
    plan(client, cookidoo_meal["id"])
    plan(client, cookidoo_meal["id"], day=1)
    plan(client, manual["id"], day=2, servings=8)
    client.post("/api/extras", json={"week_start": WEEK, "name": "Kitchen roll", "qty": ""})
    client.post("/api/list/check", json={"week_start": WEEK, "key": "bread", "checked": True})
    r = client.post("/api/cookidoo/send-list", json={"week_start": WEEK})
    assert r.json() == {"recipes": 1, "items": 2}
    assert fake_api.pushed["recipes"] == ["r10"]
    assert fake_api.pushed["items"] == ["100 g butter", "Kitchen roll"]


def test_send_list_empty_week(client):
    assert client.post("/api/cookidoo/send-list", json={"week_start": WEEK}).status_code == 400


def test_healthz_reports_build_version(client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "595eac9e1f2b3c4d")
    assert client.get("/healthz").json() == {"ok": True, "version": "595eac9"}
