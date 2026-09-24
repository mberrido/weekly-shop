WEEK = "2026-09-21"


def setup_week(client):
    curry = client.post("/api/meals", json={"name": "Curry", "servings": 4, "ingredients": [
        {"name": "chicken thighs", "qty": 500, "unit": "g"}, {"name": "carrots", "qty": 2}]}).json()
    stew = client.post("/api/meals", json={"name": "Stew", "servings": 4, "ingredients": [
        {"name": "carrot", "qty": 3}, {"name": "beef", "qty": 400, "unit": "g"}]}).json()
    pid = client.post("/api/plan", json={"week_start": WEEK, "day": 0, "slot": "dinner", "meal_id": curry["id"]}).json()["id"]
    return curry, stew, pid


def items(client):
    lst = client.get(f"/api/list?week={WEEK}").json()
    return {i["key"]: i for a in lst["aisles"] for i in a["items"]}


def remove(client, key):
    return client.post("/api/list/remove", json={"week_start": WEEK, "key": key})


def test_delete_hides_item_and_unticks(client):
    setup_week(client)
    client.post("/api/list/check", json={"week_start": WEEK, "key": "carrot", "checked": True})
    assert remove(client, "carrot").status_code == 200
    assert "carrot" not in items(client) and "chicken thigh" in items(client)
    assert client.get(f"/api/list?week={WEEK}").json()["total"] == 1


def test_comes_back_when_another_meal_needs_it(client):
    curry, stew, _ = setup_week(client)
    remove(client, "carrot")
    client.post("/api/plan", json={"week_start": WEEK, "day": 1, "slot": "dinner", "meal_id": stew["id"]})
    assert items(client)["carrot"]["qty"] == "5"


def test_comes_back_when_meal_readded_or_servings_change(client):
    curry, _, pid = setup_week(client)
    remove(client, "carrot")
    client.patch(f"/api/plan/{pid}", json={"servings": 8})
    assert items(client)["carrot"]["qty"] == "4"
    remove(client, "carrot")
    client.delete(f"/api/plan/{pid}")
    client.post("/api/plan", json={"week_start": WEEK, "day": 0, "slot": "dinner", "meal_id": curry["id"]})
    assert "carrot" in items(client)


def test_stays_deleted_otherwise_and_only_this_week(client):
    curry, _, _ = setup_week(client)
    remove(client, "carrot")
    for _ in range(2):
        assert "carrot" not in items(client)
    client.post("/api/plan", json={"week_start": "2026-09-28", "day": 0, "slot": "dinner", "meal_id": curry["id"]})
    nxt = client.get("/api/list?week=2026-09-28").json()
    assert "carrot" in {i["key"] for a in nxt["aisles"] for i in a["items"]}


def test_readding_as_extra_shows_it(client):
    setup_week(client)
    remove(client, "carrot")
    client.post("/api/extras", json={"week_start": WEEK, "name": "carrots", "qty": "1 bag"})
    extras = [i for i in items(client).values() if i["extra_id"]]
    assert [(e["name"], e["qty"]) for e in extras] == [("carrots", "1 bag")]


def test_delete_extra(client):
    setup_week(client)
    xid = client.post("/api/extras", json={"week_start": WEEK, "name": "Bin bags"}).json()["id"]
    remove(client, f"x:{xid}")
    assert f"x:{xid}" not in items(client)


def test_unknown_item(client):
    setup_week(client)
    assert remove(client, "unicorn").status_code == 404


def test_deleted_items_not_sent_to_cookidoo(client, fake_api):
    setup_week(client)
    remove(client, "carrot")
    client.post("/api/cookidoo/send-list", json={"week_start": WEEK})
    assert fake_api.pushed["items"] == ["500 g chicken thighs"]
    assert "carrot" not in items(client)   # sending didn't forget the deletion
