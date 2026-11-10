"""The Week tab's rolling 7 days: any start day, spanning two Monday weeks."""

TUE = "2026-11-10"          # a Tuesday: window is Tue 10 - Mon 16 Nov


def meal(c, name):
    return c.post("/api/meals", json={"name": name, "kinds": ["dinner"], "servings": 4}).json()["id"]


def plan_on(c, iso, meal_id, slot="dinner"):
    """Plan by calendar date, the way the Week tab does in 'from today' mode."""
    from datetime import date, timedelta
    d = date.fromisoformat(iso)
    monday = d - timedelta(days=d.weekday())
    c.post("/api/plan", json={"week_start": monday.isoformat(), "day": d.weekday(), "slot": slot, "meal_id": meal_id})


def entries(c, start=TUE):
    return [(e["date"], e["name"]) for e in c.get(f"/api/days?start={start}").json()["entries"]]


def test_window_spans_two_weeks(client):
    stew, pie = meal(client, "Stew"), meal(client, "Pie")
    plan_on(client, "2026-11-09", stew)    # Monday before: outside
    plan_on(client, "2026-11-10", stew)    # Tue
    plan_on(client, "2026-11-15", pie)     # Sun (week of 9 Nov)
    plan_on(client, "2026-11-16", pie)     # Mon (week of 16 Nov)
    plan_on(client, "2026-11-17", stew)    # Tue after: outside
    d = client.get(f"/api/days?start={TUE}").json()
    assert d["days"][0] == TUE and d["days"][-1] == "2026-11-16"
    assert entries(client) == [("2026-11-10", "Stew"), ("2026-11-15", "Pie"), ("2026-11-16", "Pie")]
    e = d["entries"][2]
    assert (e["week_start"], e["day"]) == ("2026-11-16", 0)


def test_monday_start_matches_the_week_view(client):
    stew = meal(client, "Stew")
    plan_on(client, "2026-11-12", stew)
    days = client.get("/api/days?start=2026-11-09").json()
    week = client.get("/api/week?start=2026-11-09").json()
    assert [e["id"] for e in days["entries"]] == [e["id"] for e in week["entries"]]


def test_clear_only_those_7_days(client):
    stew = meal(client, "Stew")
    for d in ["2026-11-09", "2026-11-10", "2026-11-16", "2026-11-17"]:
        plan_on(client, d, stew)
    assert client.post("/api/days/clear", json={"start": TUE}).json() == {"cleared": 2}
    assert entries(client, "2026-11-09") == [("2026-11-09", "Stew")]
    assert entries(client, "2026-11-17") == [("2026-11-17", "Stew")]


def test_copy_previous_7_days(client):
    stew, pie = meal(client, "Stew"), meal(client, "Pie")
    plan_on(client, "2026-11-03", stew)    # Tue: inside the 7 days before
    plan_on(client, "2026-11-09", pie)     # Mon: inside
    plan_on(client, "2026-11-02", pie)     # Mon before that: outside
    assert client.post("/api/days/copy-previous", json={"start": TUE}).json() == {"copied": 2}
    assert entries(client) == [("2026-11-10", "Stew"), ("2026-11-16", "Pie")]
    assert client.post("/api/days/copy-previous", json={"start": TUE}).json() == {"copied": 0}   # no repeats


def test_default_start_is_today(client):
    from datetime import date
    assert client.get("/api/days").json()["start"] == date.today().isoformat()
