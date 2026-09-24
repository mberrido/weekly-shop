"""Aisle guessing for UK supermarkets.

Each aisle has a comma-separated list of phrases. A phrase matches at the start
of a word (so 'egg' matches 'eggs' but 'ham' doesn't match 'graham'), and the
longest matching phrase across all aisles wins. That lets specific phrases
beat generic ones: 'peanut butter' (Cupboard) beats 'butter' (Dairy),
'coconut milk' beats 'milk', 'tuna steak' beats 'tuna'.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .parsing import clean_name

AISLES = [
    "Fruit & veg",
    "Meat & fish",
    "Dairy & eggs",
    "Bakery",
    "Cupboard",
    "Frozen",
    "Other",
    "Extras",
]

KEYWORDS: dict[str, str] = {
    "Fruit & veg": """
        apple, banana, orange, lemon, lime, grape, pear, peach, nectarine, plum,
        berries, strawberr, raspberr, blueberr, blackberr, cherry, cherries, mango,
        pineapple, melon, kiwi, avocado, pomegranate, clementine, satsuma,
        tomato, cherry tomato, vine tomato, potato, new potato, sweet potato,
        onion, red onion, spring onion, shallot, garlic, ginger, carrot, parsnip,
        swede, turnip, celery, celeriac, leek, courgette, aubergine, pepper,
        red pepper, bell pepper, chilli, cucumber, lettuce, salad, rocket,
        spinach, kale, cabbage, cavolo nero, broccoli, tenderstem, cauliflower,
        sprout, pea, sugar snap, mangetout, green bean, runner bean, fine bean,
        edamame, beansprout, bean sprout, asparagus, mushroom, sweetcorn,
        corn on the cob, beetroot, radish, fennel, pak choi, bok choy, squash,
        butternut, pumpkin, basil, coriander, parsley, mint, dill, chives,
        rosemary, thyme, sage, tarragon, lemongrass, watercress, herbs
    """,
    "Meat & fish": """
        chicken, chicken breast, chicken thigh, chicken wing, whole chicken,
        turkey, beef, steak, mince, minced beef, beef mince, lamb, pork, bacon,
        lardons, pancetta, ham, gammon, sausage, chorizo, salami, prosciutto,
        parma ham, duck, venison, meatball, fish, salmon, cod, haddock,
        tuna steak, prawn, shrimp, mussel, scallop, squid, mackerel, sea bass,
        trout, pollock, hake, smoked salmon, smoked haddock
    """,
    "Dairy & eggs": """
        milk, whole milk, semi-skimmed, skimmed milk, oat milk, butter,
        unsalted butter, salted butter, cream, double cream, single cream,
        whipping cream, creme fraiche, sour cream, yoghurt, yogurt,
        greek yoghurt, greek yogurt, natural yoghurt, cheese, cheddar,
        mozzarella, parmesan, grana padano, feta, halloumi, ricotta, mascarpone,
        cream cheese, soft cheese, goat's cheese, goats cheese, gruyere, brie,
        egg, fromage frais, quark, paneer, tofu, hummus, houmous, pastry,
        puff pastry, shortcrust, filo, fresh pasta, custard
    """,
    "Bakery": """
        bread, loaf, sourdough, baguette, bread roll, roll, wrap, tortilla,
        tortilla wrap, pitta, pita, naan, bagel, crumpet, croissant, brioche,
        burger bun, bun, muffin, ciabatta, focaccia, flatbread
    """,
    "Cupboard": """
        flour, plain flour, self-raising, strong white, bread flour, cornflour,
        sugar, caster sugar, icing sugar, brown sugar, honey, maple syrup,
        golden syrup, salt, black pepper, white pepper, ground pepper, peppercorn,
        cayenne pepper, oil, olive oil, vegetable oil, sesame oil, rapeseed oil,
        sunflower oil, vinegar, soy sauce, fish sauce, worcestershire, ketchup,
        tomato ketchup, mayonnaise, mayo, mustard, peanut butter, jam, marmite,
        stock, stock cube, stock pot, chicken stock, beef stock, vegetable stock,
        bouillon, gravy, rice, basmati, risotto, arborio, pasta, spaghetti, penne,
        fusilli, lasagne, tagliatelle, macaroni, orzo, noodle, rice noodle,
        couscous, bulgur, quinoa, oat, oats, rolled oats, porridge, cereal,
        granola, muesli, lentil, chickpea, beans, kidney bean, black bean,
        baked beans, butter bean, cannellini, borlotti, chopped tomatoes,
        tinned tomatoes, plum tomatoes, passata, tomato puree, tomato paste,
        sun-dried tomato, sundried tomato, coconut milk, coconut cream,
        creamed coconut, desiccated coconut, tuna, sardine, anchovy, anchovies,
        olive, caper, paprika, smoked paprika, cumin, turmeric, cinnamon, nutmeg,
        curry powder, garam masala, chilli powder, chilli flakes, oregano,
        mixed herbs, bay leaf, bay leaves, cloves, allspice, five spice, spice,
        baking powder, bicarbonate, baking soda, yeast, vanilla, cocoa,
        chocolate, chocolate chips, nuts, almond, ground almonds, almond milk,
        cashew, walnut, peanut, pine nut, pecan, hazelnut, seed, sesame seeds,
        raisin, sultana, dried fruit, crisps, biscuit, tea, coffee, gelatine,
        pesto, curry paste, harissa, tahini, sriracha, sweet chilli sauce,
        hoisin, oyster sauce, bbq sauce, breadcrumb, panko, polenta, semolina,
        cornflakes, lemon juice, lime juice, stock powder, tortilla chips,
        rice cakes, crackers, tomato sauce, pasta sauce, gnocchi, paste
    """,
    "Frozen": """
        frozen, ice cream, fish fingers, oven chips, sorbet, frozen peas,
        frozen berries, ice cubes, ice lolly
    """,
}

# Whole-word qualifiers that decide the aisle regardless of the rest.
QUALIFIERS = {"frozen": "Frozen", "tinned": "Cupboard", "canned": "Cupboard",
              "dried": "Cupboard", "jarred": "Cupboard"}


@lru_cache(maxsize=1)
def _phrases() -> list[tuple[re.Pattern[str], int, str]]:
    out = []
    for aisle, text in KEYWORDS.items():
        for phrase in (p.strip() for p in text.split(",")):
            if phrase:
                p = clean_name(phrase)
                out.append((re.compile(r"(?<![a-z])" + re.escape(p)), len(p), aisle))
    return out


def guess_aisle(name: str) -> str:
    s = clean_name(name)
    if not s:
        return "Other"
    for word in s.split():
        if word in QUALIFIERS:
            return QUALIFIERS[word]
    best_len, best = 0, "Other"
    for pattern, length, aisle in _phrases():
        if length > best_len and pattern.search(s):
            best_len, best = length, aisle
    return best
