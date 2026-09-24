"""Low Pantry Tracker items ("Add to Weekly Shop when low: N") on this week's list."""
import io
import json

import pytest

from app.pantry_client import PantryClient
from app.shopping import monday_of
from tests.conftest import PASSWORD, FakePantry

THIS_WEEK = monday_of().isoformat()


def product(id, name, qty, threshold, add):
    return {"id": id, "name": name, "quantity": qty, "reorder_threshold": threshold, "weekly_shop_qty": add}


@pytest.fixture
def pantry():
    return FakePantry([
        product(1, "Semi Skimmed Milk", 1, 2, 5),     # low, add 5
        product(2, "Saxa Fine Salt", 0, 1, 1),        # out, add 1
        product(3, "Baked Beans", 1, 2, None),        # low but no number -> left off
        product(4, "Rice", 8, 2, 3),                  # not low
    ])


@pytest.fixture
def pc(make_client, pantry):
    c = make_client(pantry=pantry)
    c.post("/login", data={"pin": PASSWORD})
    return c


def restock(client, week=THIS_WEEK):
    lst = client.get(f"/api/list?week={week}").json()
    return {i["name"]: i for a in lst["aisles"] for i in a["items"] if i.get("pantry_low")}


def test_low_items_with_a_number_go_on_this_weeks_list(pc):
    items = restock(pc)
    assert set(items) == {"Semi Skimmed Milk", "Saxa Fine Salt"}
    milk = items["Semi Skimmed Milk"]
    assert milk["qty"] == "5" and milk["aisle"] == "Dairy & eggs" and not milk["checked"]
    assert milk["pantry_low"] == {"product_id": 1, "quantity": 1}
    assert items["Saxa Fine Salt"]["qty"] == "1"


def test_only_this_week(pc):
    assert restock(pc, "2030-01-07") == {}


def test_drops_off_when_restocked(pc, pantry):
    pantry.items[0]["quantity"] = 6
    assert "Semi Skimmed Milk" not in restock(pc)


def test_tick_and_empty_basket(pc, pantry):
    pc.post("/api/list/check", json={"week_start": THIS_WEEK, "key": "p:1", "checked": True})
    assert restock(pc)["Semi Skimmed Milk"]["checked"]
    assert pc.post("/api/list/empty-basket", json={"week_start": THIS_WEEK}).json() == {"emptied": 1}
    assert "Semi Skimmed Milk" not in restock(pc)
    # Stock changes (e.g. used another one) -> it comes back.
    pantry.items[0]["quantity"] = 0
    assert "Semi Skimmed Milk" in restock(pc)


def test_delete_restock_line(pc, pantry):
    assert pc.post("/api/list/remove", json={"week_start": THIS_WEEK, "key": "p:2"}).status_code == 200
    assert "Saxa Fine Salt" not in restock(pc)
    pantry.items[1]["weekly_shop_qty"] = 2          # setting changed in the pantry app -> back
    assert restock(pc)["Saxa Fine Salt"]["qty"] == "2"
    assert pc.post("/api/list/remove", json={"week_start": THIS_WEEK, "key": "p:4"}).status_code == 404


def test_aisle_override_applies(pc):
    pc.put("/api/aisles", json={"name": "Semi Skimmed Milk", "aisle": "Other"})
    assert restock(pc)["Semi Skimmed Milk"]["aisle"] == "Other"


def test_pantry_down_means_no_restock_lines(pc, pantry):
    pantry.items = None
    assert restock(pc) == {}


def test_client_sends_read_key_and_parses_fields(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout):
        seen["key"] = req.get_header("X-pantry-key")
        return Resp(json.dumps([{"id": 1, "name": "Milk", "quantity": 1, "reorder_threshold": 2, "weekly_shop_qty": 5},
                                {"id": 2, "name": "Salt", "quantity": 3, "archived": True}]).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = PantryClient("http://pantry.test", read_key="secret-key")
    import asyncio
    products = asyncio.run(client.products())
    assert seen["key"] == "secret-key"
    assert products == [{"id": 1, "name": "Milk", "quantity": 1, "reorder_threshold": 2, "weekly_shop_qty": 5}]
