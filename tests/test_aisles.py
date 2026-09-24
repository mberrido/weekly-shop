import pytest

from app.aisles import AISLES, guess_aisle


@pytest.mark.parametrize("name,aisle", [
    ("peanut butter", "Cupboard"),
    ("butter", "Dairy & eggs"),
    ("coconut milk", "Cupboard"),
    ("milk", "Dairy & eggs"),
    ("chopped tomatoes", "Cupboard"),
    ("tomatoes", "Fruit & veg"),
    ("cherry tomatoes", "Fruit & veg"),
    ("tuna steak", "Meat & fish"),
    ("tuna steaks", "Meat & fish"),
    ("tuna", "Cupboard"),
    ("chicken thighs", "Meat & fish"),
    ("chicken stock", "Cupboard"),
    ("red onions", "Fruit & veg"),
    ("eggs", "Dairy & eggs"),
    ("wraps", "Bakery"),
    ("breadcrumbs", "Cupboard"),
    ("bread flour", "Cupboard"),
    ("frozen peas", "Frozen"),
    ("frozen spinach", "Frozen"),
    ("ice cream", "Frozen"),
    ("double cream", "Dairy & eggs"),
    ("butter beans", "Cupboard"),
    ("green beans", "Fruit & veg"),
    ("Crème fraîche", "Dairy & eggs"),
    ("dried oregano", "Cupboard"),
    ("lemon juice", "Cupboard"),
    ("lemons", "Fruit & veg"),
    ("washing-up liquid", "Other"),
    ("", "Other"),
])
def test_guess_aisle(name, aisle):
    assert guess_aisle(name) == aisle


def test_ham_needs_word_start():
    assert guess_aisle("graham crackers") == "Cupboard"


def test_aisle_order_is_uk_supermarket_order():
    assert AISLES == ["Fruit & veg", "Meat & fish", "Dairy & eggs", "Bakery",
                      "Cupboard", "Frozen", "Other", "Extras"]
