import pytest

from app.pantry_match import best_match, score
from tests.conftest import PASSWORD, FakePantry

WEEK = "2026-09-21"

# Real product names from the Pantry Tracker on the NAS, plus a few typical ones.
PRODUCTS = [
    {"id": 1, "name": "Frank's Red Hot Original", "quantity": 2},
    {"id": 2, "name": "Homepride pasta bake", "quantity": 3},
    {"id": 3, "name": "Saxa Fine Salt 750g", "quantity": 2},
    {"id": 4, "name": "Spicy Mexican Style", "quantity": 1},
    {"id": 5, "name": "Tuna Chunks in Spring Water", "quantity": 4},
    {"id": 6, "name": "Napolina Chopped Tomatoes 400g", "quantity": 6},
    {"id": 7, "name": "Chicken Stock Cubes", "quantity": 1},
    {"id": 8, "name": "Penne Pasta 500g", "quantity": 0},
    {"id": 9, "name": "Basmati Rice", "quantity": 2},
]


@pytest.mark.parametrize("ingredient,expected", [
    ("salt", "Saxa Fine Salt 750g"),
    ("tuna", "Tuna Chunks in Spring Water"),
    ("chopped tomatoes", "Napolina Chopped Tomatoes 400g"),
    ("chopped tomato", "Napolina Chopped Tomatoes 400g"),
    ("basmati rice", "Basmati Rice"),
    ("rice", "Basmati Rice"),
    ("pasta", "Penne Pasta 500g"),          # not the pasta *bake*
    ("chicken", None),                        # not chicken *stock*
    ("chicken stock", "Chicken Stock Cubes"),
    ("tomatoes", "Napolina Chopped Tomatoes 400g"),
    ("red onions", None),
    ("hot sauce", None),
])
def test_best_match(ingredient, expected):
    m = best_match(ingredient, PRODUCTS)
    assert (m["name"] if m else None) == expected


@pytest.mark.parametrize("ingredient,product,matches", [
    ("oat milk", "Tesco Oat Drink", True),
    ("oat milk", "Oatly Oat Drink Barista Edition 1L", True),
    ("soy milk", "Alpro Soya Drink", True),
    ("almond milk", "Almond Drink Unsweetened", True),
    ("rolled oats", "Tesco Oat Drink", False),
    ("oats", "Tesco Oat Drink", False),
    ("coconut", "Coconut Milk 400ml", False),
    ("coconut milk", "Coconut Milk 400ml", True),
    ("milk", "Semi Skimmed Milk 2 Pints", True),
    ("chocolate", "Chocolate Milk", False),
    # word-based, order-independent, 2+ shared words
    ("oat milk", "Tesco Oat Milk", True),
    ("chicken breast fillets", "Chicken Breasts", True),
    ("free range eggs", "Large Eggs Free Range x12", True),
    ("tinned chopped tomatoes", "Chopped Tomatoes", True),
    ("chicken stock cube", "Beef Stock Cubes", False),
    ("red wine vinegar", "White Wine Vinegar", False),
    ("unsalted butter", "Salted Butter", False),
    ("red onions", "Large onions", False),
    ("cherry tomatoes", "Chopped Tomatoes", False),
    ("smoked streaky bacon", "Unsmoked Back Bacon", False),
])
def test_plant_drinks_and_milk_words(ingredient, product, matches):
    assert (best_match(ingredient, [{"id": 1, "name": product, "quantity": 3}]) is not None) == matches


def test_closer_name_wins():
    products = [{"id": 1, "name": "Garlic & Herb Cream Cheese", "quantity": 5},
                {"id": 2, "name": "Cream Cheese", "quantity": 1}]
    assert best_match("cream cheese", products)["id"] == 2


def test_score_ignores_sizes_and_plurals():
    assert score("red onion", "Red Onions 1kg") == 1.0
    assert score("pasta", "Homepride Pasta Bake") is None


# -- list behaviour -------------------------------------------------------------

@pytest.fixture
def pantry():
    return FakePantry([dict(p) for p in PRODUCTS])


@pytest.fixture
def pclient(make_client, pantry):
    c = make_client(pantry=pantry)
    c.post("/login", data={"password": PASSWORD})
    meal = c.post("/api/meals", json={"name": "Tuna pasta", "servings": 2, "ingredients": [
        {"name": "tuna", "qty": 1, "unit": "tin"}, {"name": "pasta", "qty": 200, "unit": "g"},
        {"name": "chopped tomatoes", "qty": 1, "unit": "tin"}, {"name": "red onion", "qty": 1}]}).json()
    c.post("/api/plan", json={"week_start": WEEK, "day": 0, "slot": "dinner", "meal_id": meal["id"]})
    return c


def names(lst):
    return sorted(i["name"] for a in lst["aisles"] for i in a["items"])


def test_in_stock_items_leave_the_list(pclient):
    lst = pclient.get(f"/api/list?week={WEEK}").json()
    assert names(lst) == ["pasta", "red onion"]        # penne is at 0, so pasta stays
    assert [(p["name"], p["product"]["name"], p["auto"]) for p in lst["in_pantry"]] == [
        ("chopped tomatoes", "Napolina Chopped Tomatoes 400g", True),
        ("tuna", "Tuna Chunks in Spring Water", True)]
    assert lst["total"] == 2 and lst["pantry_ok"] is True


def test_manual_link_and_not_in_pantry(pclient):
    # Link red onion to rice (odd, but it's the user's call) -> leaves the list.
    pclient.put("/api/pantry-links", json={"name": "Red onion", "product_id": 9})
    lst = pclient.get(f"/api/list?week={WEEK}").json()
    assert "red onion" not in names(lst)
    assert next(p for p in lst["in_pantry"] if p["name"] == "red onion")["auto"] is False
    # "Not in pantry" for tuna -> back on the list despite the auto-match.
    pclient.put("/api/pantry-links", json={"name": "tuna", "product_id": None})
    assert "tuna" in names(pclient.get(f"/api/list?week={WEEK}").json())
    # Back to auto.
    pclient.put("/api/pantry-links", json={"name": "tuna", "auto": True})
    assert "tuna" not in names(pclient.get(f"/api/list?week={WEEK}").json())


def test_linked_product_out_of_stock_stays_on_list(pclient):
    pclient.put("/api/pantry-links", json={"name": "pasta", "product_id": 8})   # penne, qty 0
    assert "pasta" in names(pclient.get(f"/api/list?week={WEEK}").json())


def test_pantry_down_list_unaffected(pclient, pantry):
    pantry.items = None
    lst = pclient.get(f"/api/list?week={WEEK}").json()
    assert names(lst) == ["chopped tomatoes", "pasta", "red onion", "tuna"]
    assert lst["in_pantry"] == [] and lst["pantry_ok"] is False


def test_pantry_products_endpoint(pclient):
    r = pclient.get("/api/pantry-products").json()
    assert r["configured"] and r["ok"] and r["products"][0]["name"] == "Basmati Rice"


def test_not_configured(client):
    assert client.get("/api/config").json()["pantry"] is False
    lst = client.get(f"/api/list?week={WEEK}").json()
    assert lst["pantry_ok"] is None and lst["in_pantry"] == []
