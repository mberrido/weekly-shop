"""Match shopping-list ingredients to Pantry Tracker products by name.

Word-based, order-independent (plurals, sizes like '750g' and filler words ignored):
- Match when every ingredient word is in the product name ('tuna' ~ 'Tuna
  Chunks'), or when at least 2 ingredient words are ('chicken breast fillets'
  ~ 'Chicken Breasts', 'oat milk' ~ 'Tesco Oat Milk').
- Plant 'drinks' count as milks: 'oat milk' ~ 'Tesco Oat Drink'.
- Guards against near misses:
  * a type word the ingredient doesn't have, after the matched words, means a
    different product: 'pasta' !~ 'Pasta Bake', 'oats' !~ 'Oat Drink';
  * conflicting kinds: 'chicken stock cube' !~ 'Beef Stock Cubes',
    'red wine vinegar' !~ 'White Wine Vinegar'.
- Among candidates, the highest share of shared words wins, then most stock.
"""

from __future__ import annotations

import re

from .parsing import clean_name, merge_key

TYPE_WORDS = {
    "bake", "sauce", "stock", "soup", "seasoning", "mix", "paste", "puree", "powder",
    "granule", "flake", "oil", "juice", "vinegar", "ketchup", "crisp", "cracker", "biscuit",
    "bar", "cube", "gravy", "dressing", "spread", "drink", "cordial", "squash", "yoghurt",
    "yogurt", "flavour", "flavoured", "pot", "noodle", "kit", "rub", "marinade", "salt",
    "milk", "cream",
}
# UK plant milks are sold as "... drink" ("Tesco Oat Drink"); treat them as "... milk".
PLANT_MILKS = {"oat", "soya", "soy", "almond", "coconut", "rice", "hazelnut", "cashew", "hemp", "pea", "barista"}
SYNONYMS = {"soy": "soya"}
FILLER = {"of", "and", "&", "the", "a", "with", "in", "fresh", "large", "small", "medium", "ripe", "organic"}
CONFLICTS = [
    {"chicken", "beef", "pork", "lamb", "turkey", "duck", "fish", "vegetable", "veg", "ham"},
    {"red", "white", "green", "yellow", "brown", "black"},
    {"whole", "skimmed", "semi"},
    {"plain", "self", "strong", "wholemeal"},
    {"salted", "unsalted"},
    {"smoked", "unsmoked"},
]
_SIZE = re.compile(r"^\d+([.,]\d+)?(g|kg|ml|l|cl|x|pk|pack)?$")


def _words(text: str) -> list[str]:
    words = [merge_key(w) for w in clean_name(text).replace("-", " ").split() if not _SIZE.match(w)]
    words = [SYNONYMS.get(w, w) for w in words]
    return ["milk" if w == "drink" and i and words[i - 1] in PLANT_MILKS else w for i, w in enumerate(words)]


def score(ingredient: str, product_name: str) -> float | None:
    """Higher is better; None means no match."""
    ing = [w for w in _words(ingredient) if w not in FILLER]
    prod = [w for w in _words(product_name) if w not in FILLER]
    if not ing or not prod:
        return None
    ing_set, prod_set = set(ing), set(prod)
    shared = ing_set & prod_set
    if not (shared == ing_set or len(shared) >= 2):
        return None
    # A different kind of product: a type word we didn't ask for, after the match.
    # Skipped when the ingredient already names a product type ('chicken stock' ~ 'Chicken Stock Cubes').
    first = min(prod.index(w) for w in shared)
    if ing[-1] not in TYPE_WORDS and any(w in TYPE_WORDS and w not in ing_set for w in prod[first + 1:]):
        return None
    # Conflicting kinds (chicken vs beef, red vs white ...).
    for group in CONFLICTS:
        mine, theirs = ing_set & group, prod_set & group
        if mine and theirs and not (mine & theirs):
            return None
    return len(shared) / len(ing_set | prod_set)


def best_match(ingredient: str, products: list[dict]) -> dict | None:
    best, best_key = None, None
    for p in products:
        s = score(ingredient, p.get("name") or "")
        if s is None:
            continue
        key = (s, p.get("quantity") or 0)
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best
