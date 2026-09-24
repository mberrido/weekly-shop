import pytest

from app.parsing import format_qty, merge_key, normalise, parse_amount, parse_line, parse_quantity


@pytest.mark.parametrize("text,qty", [
    ("2", 2), ("200", 200), ("1.5", 1.5), ("1,5", 1.5),
    ("1 - 2", 2), ("1-2", 2), ("2–3", 3), ("1 to 2", 2),
    ("½", 0.5), ("¼", 0.25), ("¾", 0.75), ("1½", 1.5), ("1 ½", 1.5),
    ("1/2", 0.5), ("1 1/2", 1.5), ("⅓", 1 / 3), ("⅔", 2 / 3), ("½ - 1", 1),
])
def test_parse_quantity(text, qty):
    value, rest = parse_quantity(text)
    assert value == pytest.approx(qty)
    assert rest == ""


def test_parse_quantity_none():
    assert parse_quantity("wraps") == (None, "wraps")
    assert parse_quantity("") == (None, "")


@pytest.mark.parametrize("line,expected", [
    ("500 g chicken thighs", ("chicken thighs", 500, "g", "")),
    ("200g flour", ("flour", 200, "g", "")),
    ("2 red onions", ("red onions", 2, "", "")),
    ("wraps", ("wraps", None, "", "")),
    ("1.5 kg potatoes", ("potatoes", 1500, "g", "")),
    ("1 l milk", ("milk", 1000, "ml", "")),
    ("2 pinches salt", ("salt", 2, "pinch", "")),
    ("2 tbsp of olive oil", ("olive oil", 2, "tbsp", "")),
    ("3 cloves garlic, crushed", ("garlic", 3, "clove", "crushed")),
    ("2 leeks", ("leeks", 2, "", "")),
    ("1½ tsp cumin", ("cumin", 1.5, "tsp", "")),
])
def test_parse_line(line, expected):
    r = parse_line(line)
    assert (r["name"], r["qty"], r["unit"], r["note"]) == pytest.approx(expected)


def test_parse_amount_cookidoo_descriptions():
    assert parse_amount("200 g") == (200, "g", "")
    assert parse_amount("1 - 2") == (2, "", "")
    assert parse_amount("½ tsp") == (0.5, "tsp", "")
    assert parse_amount("0,5 kg") == (500, "g", "")
    assert parse_amount("") == (None, "", "")
    assert parse_amount("to taste") == (None, "", "to taste")


def test_normalise_units():
    assert normalise(2, "kg") == (2000, "g")
    assert normalise(1.5, "l") == (1500, "ml")
    assert normalise(3, "pinches") == (3, "pinch")
    assert normalise(1, "Tablespoons") == (1, "tbsp")
    assert normalise(None, "g") == (None, "g")
    assert normalise(2, "") == (2, "")


@pytest.mark.parametrize("qty,unit,out", [
    (300, "g", "300 g"), (1000, "g", "1 kg"), (1250, "g", "1.25 kg"), (1500, "ml", "1.5 l"),
    (333.3333, "g", "333 g"), (2, "", "2"), (1.5, "", "1.5"), (1, "pinch", "1 pinch"),
    (2, "pinch", "2 pinches"), (0.5, "tsp", "0.5 tsp"), (2000, "ml", "2 l"),
])
def test_format_qty(qty, unit, out):
    assert format_qty(qty, unit) == out


def test_merge_key_singularises_last_word():
    assert merge_key("Red Onions") == merge_key("red onion") == "red onion"
    assert merge_key("tomatoes") == "tomato"
    assert merge_key("berries") == "berry"
    assert merge_key("hummus") == "hummus"
    assert merge_key("Crème fraîche") == "creme fraiche"
