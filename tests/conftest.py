from types import SimpleNamespace as NS

import pytest

from app.config import load_settings
from app.cookidoo_service import CookidooService
from app.main import create_app
from tests.asgi_client import Client

PASSWORD = "482913"   # the household PIN
SECRET = "s" * 48


class FakeCookidooApi:
    """Stands in for cookidoo_api.Cookidoo with the methods the app uses."""

    def __init__(self):
        self.fail = None
        self.pushed = {}
        self.calls = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    async def get_recipe_details(self, id):
        self._maybe_fail()
        self.calls.append(("details", id))
        return NS(
            id=id, name=f"Recipe {id}", serving_size=4, url=f"https://cookidoo.co.uk/recipes/recipe/en-GB/{id}",
            thumbnail="https://img/thumb.jpg", image="https://img/big.jpg",
            ingredients=[NS(name="chicken thighs, diced", description="500 g"),
                         NS(name="red onions", description="1 - 2"),
                         NS(name="salt", description="1 pinch"),
                         NS(name="coriander", description="")],
        )

    async def get_custom_recipe(self, id):
        self._maybe_fail()
        return NS(id=id, name="Gran's flapjack", serving_size=8, url="u", thumbnail=None, image=None,
                  ingredients=["200 g oats", "100g butter", "golden syrup"])

    async def search_recipes(self, query, page_size=None):
        self._maybe_fail()
        return NS(total=1, recipes=[NS(id="r1", name=f"{query} pie", thumbnail=None, url="u")])

    async def get_recipes_in_calendar_week(self, day):
        self._maybe_fail()
        return [
            NS(id="2026-09-21", title="", recipes=[NS(id="r100", name="Mon")], customer_recipe_ids=[]),
            NS(id="2026-09-23", title="", recipes=[NS(id="r200", name="Wed")], customer_recipe_ids=["01ABCDEFGHIJ"]),
        ]

    async def add_ingredient_items_for_recipes(self, ids):
        self._maybe_fail()
        self.pushed["recipes"] = ids

    async def add_ingredient_items_for_custom_recipes(self, ids):
        self.pushed["custom"] = ids

    async def add_additional_items(self, names):
        self.pushed["items"] = names


@pytest.fixture
def fake_api():
    return FakeCookidooApi()


@pytest.fixture
def settings(tmp_path):
    return load_settings({"DB_PATH": str(tmp_path / "shop.db"), "APP_PIN": PASSWORD,
                          "SESSION_SECRET": SECRET})


@pytest.fixture
def make_client(settings, fake_api):
    clients = []

    def make(cookidoo=None, pantry=None, photos=None, **settings_overrides):
        async def factory():
            return fake_api
        svc = cookidoo if cookidoo is not None else CookidooService("", "", api_factory=factory)
        s = settings.__class__(**{**settings.__dict__, **settings_overrides})
        c = Client(create_app(s, svc, run_backups=False, pantry=pantry, photos=photos))
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.close()


@pytest.fixture
def anon(make_client):
    """A client that hasn't logged in."""
    return make_client()


@pytest.fixture
def client(make_client):
    c = make_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 303, r.text
    return c


class FakePantry:
    """Stands in for PantryClient; set .items to None to simulate it being down."""

    configured = True

    def __init__(self, items):
        self.items = items

    async def products(self):
        return self.items
