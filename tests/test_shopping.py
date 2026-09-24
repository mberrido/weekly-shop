import pytest

from app.shopping import PlannedMeal, merge_items, monday_of


def ing(name, qty=None, unit=""):
    return {"name": name, "qty": qty, "unit": unit}


def test_scales_by_servings():
    items = merge_items([PlannedMeal("Curry", 6 / 4, [ing("chicken thighs", 500, "g")])], set())
    assert items["chicken thigh"].qty == "750 g"


def test_merges_units_and_names_across_meals():
    planned = [
        PlannedMeal("Curry", 1, [ing("Red onions", 2), ing("rice", 0.5, "kg")]),
        PlannedMeal("Chilli", 1, [ing("red onion", 1), ing("rice", 300, "g")]),
    ]
    items = merge_items(planned, set())
    assert items["red onion"].qty == "3"
    assert items["red onion"].meals == ["Curry", "Chilli"]
    assert items["rice"].qty == "800 g"


def test_kg_display_and_mixed_some():
    planned = [
        PlannedMeal("A", 1, [ing("flour", 700, "g")]),
        PlannedMeal("B", 1, [ing("flour", 0.5, "kg"), ing("wraps")]),
        PlannedMeal("C", 1, [ing("flour")]),
    ]
    items = merge_items(planned, set())
    assert items["flour"].qty == "1.2 kg + some"
    assert items["wrap"].qty == ""


def test_mixed_units_same_name():
    items = merge_items([PlannedMeal("A", 1, [ing("butter", 50, "g"), ing("butter", 1, "tbsp")])], set())
    assert items["butter"].qty == "50 g + 1 tbsp"


def test_pantry_items_skipped():
    planned = [PlannedMeal("A", 1, [ing("Salt", 1, "pinch"), ing("Ice cube", 4), ing("pasta", 200, "g")])]
    items = merge_items(planned, {"salt", "ice cube"})
    assert list(items) == ["pasta"]


def test_pinch_plurals_merge():
    items = merge_items([PlannedMeal("A", 2, [ing("nutmeg", 1, "pinch")]),
                         PlannedMeal("B", 1, [ing("nutmeg", 1, "pinches")])], set())
    assert items["nutmeg"].qty == "3 pinches"


def test_monday_of():
    assert monday_of("2026-09-27").isoformat() == "2026-09-21"
    assert monday_of("2026-09-21").isoformat() == "2026-09-21"
