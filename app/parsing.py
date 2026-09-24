"""Ingredient quantity parsing, unit normalisation and display formatting.

Kept dependency-free so it is easy to unit test. The client-side parser in
index.html mirrors `parse_line` for the meal editor preview.
"""

from __future__ import annotations

import re
import unicodedata

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}
_FRAC_CHARS = "".join(FRACTIONS)

# One number: "1½", "1 ½", "1 1/2", "1/2", "1,5", "1.5", "2", "½"
_NUM = (
    rf"\d+\s*[{_FRAC_CHARS}]"
    r"|\d+\s+\d+/\d+"
    r"|\d+/\d+"
    r"|\d+(?:[.,]\d+)?"
    rf"|[{_FRAC_CHARS}]"
)
_QTY_RE = re.compile(
    rf"^\s*(?P<a>{_NUM})(?:\s*(?:-|–|—|to)\s*(?P<b>{_NUM}))?\s*", re.IGNORECASE
)

# alias -> (canonical unit, multiplier)
_UNIT_ALIASES: dict[str, tuple[str, float]] = {}
for _canon, _mult, _aliases in [
    ("g", 1, "g gr gram grams gramme grammes"),
    ("g", 1000, "kg kgs kilo kilos kilogram kilograms"),
    ("ml", 1, "ml millilitre millilitres milliliter milliliters"),
    ("ml", 10, "cl"),
    ("ml", 100, "dl"),
    ("ml", 1000, "l ltr litre litres liter liters"),
    ("tsp", 1, "tsp tsps teaspoon teaspoons"),
    ("tbsp", 1, "tbsp tbsps tbs tablespoon tablespoons"),
    ("pinch", 1, "pinch pinches"),
    ("clove", 1, "clove cloves"),
    ("tin", 1, "tin tins can cans"),
    ("pack", 1, "pack packs packet packets"),
    ("bunch", 1, "bunch bunches"),
    ("slice", 1, "slice slices"),
    ("sprig", 1, "sprig sprigs"),
    ("handful", 1, "handful handfuls"),
    ("jar", 1, "jar jars"),
    ("bag", 1, "bag bags"),
]:
    for _a in _aliases.split():
        _UNIT_ALIASES[_a] = (_canon, float(_mult))

_UNIT_RE = re.compile(
    r"^(?P<u>"
    + "|".join(sorted((re.escape(a) for a in _UNIT_ALIASES), key=len, reverse=True))
    + r")\.?(?![a-z])\s*(?:of\s+)?",
    re.IGNORECASE,
)

_PLURAL_UNITS = {
    "pinch": "pinches", "clove": "cloves", "tin": "tins", "pack": "packs",
    "bunch": "bunches", "slice": "slices", "sprig": "sprigs",
    "handful": "handfuls", "jar": "jars", "bag": "bags",
}


def _num(tok: str) -> float:
    tok = tok.strip()
    if tok[-1] in FRACTIONS:
        whole = tok[:-1].strip()
        return (float(whole) if whole else 0.0) + FRACTIONS[tok[-1]]
    if "/" in tok:
        parts = tok.split()
        whole = float(parts[0]) if len(parts) == 2 else 0.0
        n, d = parts[-1].split("/")
        return whole + (float(n) / float(d) if float(d) else 0.0)
    return float(tok.replace(",", "."))


def parse_quantity(text: str) -> tuple[float | None, str]:
    """Pull a leading quantity off `text`. Ranges return the upper bound."""
    m = _QTY_RE.match(text or "")
    if not m:
        return None, (text or "").strip()
    value = _num(m.group("b") or m.group("a"))
    return value, text[m.end():].strip()


def split_unit(text: str) -> tuple[str, str]:
    """Pull a leading unit off `text`; returns (canonical-ish alias, rest)."""
    m = _UNIT_RE.match(text or "")
    if not m:
        return "", (text or "").strip()
    return m.group("u").lower(), text[m.end():].strip()


def normalise(qty: float | None, unit: str) -> tuple[float | None, str]:
    """Convert to canonical unit (kg->g, l->ml, pinches->pinch...)."""
    u = (unit or "").strip().lower().rstrip(".")
    if u in _UNIT_ALIASES:
        canon, mult = _UNIT_ALIASES[u]
        return (qty * mult if qty is not None else None), canon
    return qty, u


def split_note(name: str) -> tuple[str, str]:
    """'red onions, finely chopped' -> ('red onions', 'finely chopped')."""
    name = (name or "").strip()
    if "," in name:
        head, tail = name.split(",", 1)
        return head.strip(), tail.strip()
    return name, ""


def parse_amount(description: str) -> tuple[float | None, str, str]:
    """Parse a Cookidoo-style amount like '200 g', '1 - 2', '½ tsp'.

    Returns (qty, canonical unit, leftover text for the note).
    """
    qty, rest = parse_quantity(description or "")
    unit, rest = split_unit(rest) if qty is not None else ("", rest)
    qty, unit = normalise(qty, unit)
    return qty, unit, rest


def parse_line(line: str) -> dict:
    """Parse a free-text line like '500 g chicken thighs, diced'."""
    qty, rest = parse_quantity(line or "")
    unit = ""
    if qty is not None:
        unit, rest = split_unit(rest)
    qty, unit = normalise(qty, unit)
    name, note = split_note(rest)
    return {"name": name, "qty": qty, "unit": unit, "note": note}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def clean_name(name: str) -> str:
    """Lowercase, accent-free, single-spaced name."""
    s = _strip_accents((name or "").lower())
    s = re.sub(r"[^\w\s&'-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _singular(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def merge_key(name: str) -> str:
    """Key used to merge list items and to match pantry/aisle overrides.

    Lowercased and singularised on the last word so '2 red onions' and
    '1 red onion' land on the same line.
    """
    s = clean_name(name)
    if not s:
        return ""
    words = s.split(" ")
    words[-1] = _singular(words[-1])
    return " ".join(words)


def fmt_num(x: float, decimals: int = 2) -> str:
    s = f"{round(x, decimals):.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def format_qty(qty: float, unit: str) -> str:
    """Human display: 1250 g -> '1.25 kg', 2 pinch -> '2 pinches'."""
    if unit == "g" and qty >= 1000:
        return f"{fmt_num(qty / 1000)} kg"
    if unit == "ml" and qty >= 1000:
        return f"{fmt_num(qty / 1000)} l"
    if unit in ("g", "ml"):
        return f"{fmt_num(qty, 0)} {unit}"
    n = fmt_num(qty)
    if not unit:
        return n
    if unit in _PLURAL_UNITS and qty > 1:
        return f"{n} {_PLURAL_UNITS[unit]}"
    return f"{n} {unit}"
