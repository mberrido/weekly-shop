"""Send to Skylight, against an in-memory fake of Skylight's private API."""
import base64
import hashlib
import json
import urllib.parse

import pytest

from app.skylight import Response, SkylightService
from tests.conftest import PASSWORD

WEEK = "2026-09-21"
EMAIL, SKY_PW = "family@example.com", "sky-secret"


class FakeSkylight:
    """Just enough of app.ourskylight.com: OAuth+PKCE web login, frames, meal categories,
    recipes and sittings (with per-date instance deletes)."""

    def __init__(self):
        self.logins = 0
        self.valid_tokens = set()
        self.pending = {}            # code -> (challenge, state)
        self.state = None
        self.challenge = None
        self.recipes = {"r1": "Beef stew"}          # already in Skylight's recipe box
        self.sittings = {}                           # id -> {date, cat, recipe}
        self.next_id = 100
        self.fail_api = None

    def _id(self):
        self.next_id += 1
        return str(self.next_id)

    def __call__(self):           # transport factory
        return self.send

    def send(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        form = urllib.parse.parse_qs(body.decode()) if body and headers.get("Content-Type", "").startswith("application/x-www") else {}
        if path == "/oauth/authorize":
            self.state, self.challenge = q["state"][0], q["code_challenge"][0]
            return Response(302, {"location": "/login"}, b"")
        if path == "/login":
            return Response(200, {}, b'<form><input type="hidden" name="authenticity_token" value="csrf-123"></form>')
        if path == "/auth/session":
            if form.get("authenticity_token") != ["csrf-123"] or form.get("email") != [EMAIL] or form.get("password") != [SKY_PW]:
                return Response(302, {"location": "/login?error=1"}, b"")
            self.pending["code-1"] = (self.challenge, self.state)
            return Response(302, {"location": f"skylight-family://welcome?code=code-1&state={self.state}"}, b"")
        if path == "/oauth/token":
            if form["grant_type"] == ["authorization_code"]:
                challenge, _ = self.pending.pop(form["code"][0])
                verifier = form["code_verifier"][0]
                if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() != challenge:
                    return Response(400, {}, b"{}")
                self.logins += 1
            tok = f"tok-{self.logins}-{len(self.valid_tokens)}"
            self.valid_tokens.add(tok)
            return Response(200, {}, json.dumps({"access_token": tok, "refresh_token": "ref", "expires_in": 7200,
                                                 "created_at": 4_000_000_000}).encode())
        # data API
        if headers.get("Authorization", "").removeprefix("Bearer ") not in self.valid_tokens:
            return Response(401, {}, b"{}")
        if self.fail_api:
            return Response(self.fail_api, {}, b"{}")
        data = json.loads(body) if body else {}
        if path == "/api/frames":
            return Response(200, {}, json.dumps({"data": [{"id": "77", "type": "frame"}]}).encode())
        base = "/api/frames/77/meals"
        if path == f"{base}/categories":
            return Response(200, {}, json.dumps({"data": [
                {"id": "c1", "type": "meal_category", "attributes": {"label": "Breakfast"}},
                {"id": "c3", "type": "meal_category", "attributes": {"label": "Dinner"}}]}).encode())
        if path == f"{base}/recipes" and method == "GET":
            return Response(200, {}, json.dumps({"data": [{"id": k, "attributes": {"summary": v}} for k, v in self.recipes.items()]}).encode())
        if path == f"{base}/recipes" and method == "POST":
            rid = self._id(); self.recipes[rid] = data["summary"]
            return Response(201, {}, json.dumps({"data": {"id": rid, "attributes": {"summary": data["summary"]}}}).encode())
        if path == f"{base}/sittings" and method == "GET":
            lo, hi = q["date_min"][0], q["date_max"][0]
            out = [{"id": sid, "attributes": {"instances": [s["date"]]},
                    "relationships": {"meal_category": {"data": {"id": s["cat"]}}, "meal_recipe": {"data": {"id": s["recipe"]}}}}
                   for sid, s in self.sittings.items() if lo <= s["date"] <= hi]
            return Response(200, {}, json.dumps({"data": out}).encode())
        if path == f"{base}/sittings" and method == "POST":
            sid = self._id()
            self.sittings[sid] = {"date": data["date"], "cat": data["meal_category_id"], "recipe": data["meal_recipe_id"]}
            return Response(201, {}, json.dumps({"data": [{"id": sid}]}).encode())
        if method == "DELETE" and path.startswith(f"{base}/sittings/") and "/instances/" in path:
            sid = path.split("/sittings/")[1].split("/")[0]
            self.sittings.pop(sid, None)
            return Response(204, {}, b"")
        return Response(404, {}, b"{}")

    def plan(self):
        """What's on the Skylight: date -> sorted dinner names."""
        out = {}
        for s in self.sittings.values():
            if s["cat"] == "c3":
                out.setdefault(s["date"], []).append(self.recipes[s["recipe"]])
        return {d: sorted(v) for d, v in sorted(out.items())}


@pytest.fixture
def sky():
    return FakeSkylight()


@pytest.fixture
def sc(make_client, sky, tmp_path):
    svc = SkylightService(EMAIL, SKY_PW, tmp_path / "skylight_token.json", transport_factory=sky)
    c = make_client(skylight=svc)
    c.post("/login", data={"pin": PASSWORD})
    return c


def meal(c, name):
    return c.post("/api/meals", json={"name": name, "kinds": ["dinner", "lunch"], "servings": 4}).json()["id"]


def plan(c, meal_id, day, slot="dinner", week=WEEK):
    c.post("/api/plan", json={"week_start": week, "day": day, "slot": slot, "meal_id": meal_id})


def send(c, week=WEEK):
    return c.post("/api/skylight/send-week", json={"week_start": week})


def preview(c, week=WEEK):
    return {d["date"]: d["status"] for d in c.post("/api/skylight/preview", json={"week_start": week}).json()["days"]}


def on_skylight(sky, date, name):
    """Put a dinner straight onto the (fake) Skylight, as if added there."""
    rid = next((k for k, v in sky.recipes.items() if v == name), None) or sky._id()
    sky.recipes[rid] = name
    sky.sittings[sky._id()] = {"date": date, "cat": "c3", "recipe": rid}


def test_sends_dinners_to_empty_days_by_name(sc, sky):
    stew, curry = meal(sc, "Beef stew"), meal(sc, "Chicken curry")
    plan(sc, stew, 0)
    plan(sc, curry, 2)
    plan(sc, curry, 3, slot="lunch")                 # lunches stay out
    plan(sc, stew, 1, week="2026-09-28")            # other weeks stay out
    r = send(sc)
    assert r.status_code == 200 and r.json() == {"added": 2, "already_there": 0, "kept": 0, "replaced": 0}
    assert sky.plan() == {"2026-09-21": ["Beef stew"], "2026-09-23": ["Chicken curry"]}
    assert sorted(sky.recipes.values()) == ["Beef stew", "Chicken curry"]   # stew reused, curry created


def test_days_with_nothing_planned_are_left_alone(sc, sky):
    """Leftover meals moved to Tue/Wed on the Skylight survive sending Thu/Fri."""
    on_skylight(sky, "2026-09-22", "Lasagne")
    on_skylight(sky, "2026-09-23", "Fish pie")
    plan(sc, meal(sc, "Fajitas"), 3)
    plan(sc, meal(sc, "Pizza"), 4)
    assert preview(sc) == {"2026-09-22": "untouched", "2026-09-23": "untouched",
                           "2026-09-24": "add", "2026-09-25": "add"}
    assert send(sc).json()["added"] == 2
    assert sky.plan() == {"2026-09-22": ["Lasagne"], "2026-09-23": ["Fish pie"],
                          "2026-09-24": ["Fajitas"], "2026-09-25": ["Pizza"]}


def test_same_dinner_already_there_is_not_duplicated(sc, sky):
    plan(sc, meal(sc, "Beef stew"), 0)
    send(sc)
    assert preview(sc) == {"2026-09-21": "same"}
    assert send(sc).json() == {"added": 0, "already_there": 1, "kept": 0, "replaced": 0}
    assert sky.plan() == {"2026-09-21": ["Beef stew"]}


def test_conflict_keeps_skylight_unless_told_otherwise(sc, sky):
    on_skylight(sky, "2026-09-22", "Lasagne")
    on_skylight(sky, "2026-09-23", "Fish pie")
    curry = meal(sc, "Chicken curry")
    plan(sc, curry, 1)
    plan(sc, curry, 2)
    days = sc.post("/api/skylight/preview", json={"week_start": WEEK}).json()["days"]
    assert days[0] == {"date": "2026-09-22", "app": ["Chicken curry"], "skylight": ["Lasagne"], "status": "conflict"}
    r = sc.post("/api/skylight/send-week", json={"week_start": WEEK, "choices": {"2026-09-23": "app"}})
    assert r.json() == {"added": 0, "already_there": 0, "kept": 1, "replaced": 1}
    assert sky.plan() == {"2026-09-22": ["Lasagne"], "2026-09-23": ["Chicken curry"]}


def test_empty_week_changes_nothing(sc, sky):
    on_skylight(sky, "2026-09-22", "Lasagne")
    assert send(sc).json() == {"added": 0, "already_there": 0, "kept": 0, "replaced": 0}
    assert sky.plan() == {"2026-09-22": ["Lasagne"]}


def test_other_meal_times_on_skylight_are_ignored(sc, sky):
    sky.sittings["900"] = {"date": "2026-09-21", "cat": "c1", "recipe": "r1"}   # a breakfast
    plan(sc, meal(sc, "Chicken curry"), 0)
    assert preview(sc) == {"2026-09-21": "add"}
    send(sc)
    assert "900" in sky.sittings


def test_bad_choice_rejected(sc):
    r = sc.post("/api/skylight/send-week", json={"week_start": WEEK, "choices": {"2026-09-22": "both"}})
    assert r.status_code == 422


def test_signs_in_once_then_reuses_the_saved_token(sc, sky, tmp_path):
    plan(sc, meal(sc, "Beef stew"), 0)
    send(sc); send(sc)
    assert sky.logins == 1
    saved = json.loads((tmp_path / "skylight_token.json").read_text())
    assert saved["access_token"] in sky.valid_tokens and SKY_PW not in json.dumps(saved)


def test_signs_in_again_when_the_token_is_revoked(sc, sky):
    plan(sc, meal(sc, "Beef stew"), 0)
    send(sc)
    sky.valid_tokens.clear()
    assert send(sc).status_code == 200 and sky.logins == 2


def test_wrong_password(make_client, sky, tmp_path):
    svc = SkylightService(EMAIL, "nope", tmp_path / "t.json", transport_factory=sky)
    c = make_client(skylight=svc)
    c.post("/login", data={"pin": PASSWORD})
    r = send(c)
    assert r.status_code == 502 and "didn't accept the email or password" in r.json()["detail"]
    assert "nope" not in r.text


def test_skylight_errors_become_502(sc, sky):
    plan(sc, meal(sc, "Beef stew"), 0)
    sky.fail_api = 500
    r = send(sc)
    assert r.status_code == 502 and "Skylight returned an error" in r.json()["detail"]
    assert sc.get(f"/api/week?start={WEEK}").status_code == 200        # rest of the app fine


def test_not_configured(client):
    assert client.get("/api/config").json()["skylight"] is False
    r = client.post("/api/skylight/send-week", json={"week_start": WEEK})
    assert r.status_code == 503 and "isn't set up" in r.json()["detail"]
