"""The tour (docs/demo.md): seven steps per visitor. Client-only steps need the post; the
others are read from the world: a pull request by this visitor, their trip (or the one in
force they were shown), the verified response on that incident. Skip works on each; a
second visitor starts at step 1; outside demo mode the routes are not there. A story
(drift, honeytoken, cost) is an order, written once with the source the visitor arrived
from; a product registers steps of its own and a story may name them."""

from __future__ import annotations

from typing import Any, Dict, List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api.routes import demo as demo_routes
from compass_core import demo
from compass_core.keys import honeypot_incident_sort_key, workspace_pk

WS = "ws-demo"
PK = workspace_pk(WS)
CONFIG: Dict[str, Any] = {"workspace_id": WS, "org_id": "acme", "dynamodb_config": {"workspace_id": WS, "items": "items", "map": "map"},
                          "aws": {"account_id": "123456789012"}, "demo": {"trippable": ["tok-1"]}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("APP_MODE", "demo")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        actor: Dict[str, Any] = {"workspace_id": WS, "org_id": "acme", "user_id": "visitor-1"}
        prs: List[Dict[str, Any]] = []
        monkeypatch.setattr(demo_routes, "get_actor_ids", lambda request: dict(actor))
        monkeypatch.setattr(demo_routes, "get_workspace_config", lambda request: dict(CONFIG))
        monkeypatch.setattr(demo_routes, "get_workspace_dynamodb_config", lambda request: dict(CONFIG["dynamodb_config"]))
        monkeypatch.setattr(demo_routes, "_prs", lambda request: list(prs))
        app = FastAPI()
        app.include_router(demo_routes.router)
        yield TestClient(app), table, actor, prs


def _ids(tour: Dict[str, Any], key: str = "done") -> List[str]:
    return [s["id"] for s in tour["steps"] if s[key]]


def test_the_tour_completes_from_events_and_from_the_world(env):
    client, table, actor, prs = env
    tour = client.get("/api/demo/tour").json()
    assert [s["id"] for s in tour["steps"]] == ["map", "unmanaged", "import_pr", "decoys", "trip", "response", "run_it"]
    assert tour["completed"] == 0 and tour["current"] == "map" and tour["finished"] is False
    assert tour["steps"][0]["detail"]["search"] == "payments" and tour["steps"][2]["detail"]["search"] == "acme-legacy-exports"
    assert tour["steps"][3]["detail"]["token"] == "tok-1" and tour["steps"][6]["detail"]["links"]["install"]

    # client-only steps need the post; an api step posted stays not done until the world says so
    assert _ids(client.post("/api/demo/tour/map").json()) == ["map"]
    assert _ids(client.post("/api/demo/tour/unmanaged").json()) == ["map", "unmanaged"]
    assert client.post("/api/demo/tour/nope").status_code == 404
    tour = client.get("/api/demo/tour").json()
    assert tour["current"] == "import_pr" and tour["completed"] == 2

    # a pull request by this visitor
    prs.append({"repo": "acme/infra", "number": 7, "pr_url": "https://github.com/acme/infra/pull/7", "origin": {"captain": "iac", "command": "import", "user_id": "visitor-2"}})
    assert "import_pr" not in _ids(client.get("/api/demo/tour").json()), "another visitor's"
    prs.append({"repo": "acme/infra", "number": 8, "pr_url": "https://github.com/acme/infra/pull/8", "origin": {"captain": "iac", "command": "import", "user_id": "visitor-1"}})
    tour = client.get("/api/demo/tour").json()
    assert "import_pr" in _ids(tour) and tour["steps"][2]["detail"]["pr_url"] == "https://github.com/acme/infra/pull/8"

    # the decoys viewed; the trip: this visitor's own, with its incident
    client.post("/api/demo/tour/decoys")
    demo.claim_trip(table, PK, "tok-1", user_id="visitor-1", cooldown_seconds=300, now_ms=demo.epoch_ms())
    assert "trip" not in _ids(client.get("/api/demo/tour").json()), "claimed, not fired yet"
    demo.record_trip(table, PK, "tok-1", fired_at="2026-10-21T10:00:00Z", incident_id="inc-1")
    table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-1"), "incident_id": "inc-1", "status": "open", "honeytoken_ids": ["tok-1"]})
    tour = client.get("/api/demo/tour").json()
    assert "trip" in _ids(tour) and tour["steps"][4]["detail"]["incident_id"] == "inc-1" and tour["steps"][4]["detail"]["by"] == "you"
    assert "response" not in _ids(tour) and tour["steps"][5]["detail"]["incident_id"] == "inc-1"

    # the response verified; the last link clicked: the tour is done
    table.update_item(Key={"pk": PK, "sk": honeypot_incident_sort_key("inc-1")}, UpdateExpression="SET action_verified_at = :t", ExpressionAttributeValues={":t": "2026-10-21T10:01:00Z"})
    tour = client.post("/api/demo/tour/run_it").json()
    assert tour["completed"] == 7 and tour["finished"] is True and tour["current"] is None


def test_a_visitor_shown_the_trip_in_force_has_tripped_and_a_second_visitor_starts_at_step_one(env):
    client, table, actor, prs = env
    demo.claim_trip(table, PK, "tok-1", user_id="visitor-9", cooldown_seconds=300, now_ms=demo.epoch_ms())
    demo.record_trip(table, PK, "tok-1", fired_at="2026-10-21T10:00:00Z", incident_id="inc-9")
    table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-9"), "incident_id": "inc-9", "status": "open", "action_verified_at": "2026-10-21T10:01:00Z"})
    before = client.get("/api/demo/tour").json()
    assert "trip" not in _ids(before) and "response" not in _ids(before), "someone else's trip is not yours until you were shown it"
    shown = client.post("/api/demo/tour/trip", json={"incident_id": "inc-9"}).json()
    assert "trip" in _ids(shown) and "response" in _ids(shown)
    assert shown["steps"][4]["detail"]["by"] == "a visitor" and shown["steps"][5]["detail"]["incident_id"] == "inc-9"

    # the visitor trips on their own: until their alert lands, the step is theirs with no incident yet,
    # never the other visitor's incident in its place; then it is their own
    demo.record_trip(table, PK, "tok-1", **{"until": 1})
    demo.claim_trip(table, PK, "tok-1", user_id="visitor-1", cooldown_seconds=300, now_ms=demo.epoch_ms())
    demo.record_trip(table, PK, "tok-1", fired_at="2026-10-21T11:00:00Z")
    mine = client.get("/api/demo/tour").json()
    assert mine["steps"][4]["done"] and mine["steps"][4]["detail"]["by"] == "you" and mine["steps"][4]["detail"]["incident_id"] is None
    assert not mine["steps"][5]["done"] and mine["steps"][5]["detail"]["incident_id"] is None
    demo.record_trip(table, PK, "tok-1", incident_id="inc-10")
    table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-10"), "incident_id": "inc-10", "status": "open"})
    own = client.get("/api/demo/tour").json()
    assert own["steps"][4]["detail"]["incident_id"] == "inc-10" and not own["steps"][5]["done"]

    # skip works on each step, and counts towards the end but not as done
    skipped = client.post("/api/demo/tour/map/skip").json()
    assert "map" in _ids(skipped, "skipped") and "map" not in _ids(skipped) and skipped["current"] == "unmanaged"

    # another visitor: their own tour, at step one
    actor["user_id"] = "visitor-2"
    fresh = client.get("/api/demo/tour").json()
    assert fresh["completed"] == 0 and fresh["current"] == "map" and not _ids(fresh, "skipped")


def test_outside_demo_mode_the_tour_is_not_there(env, monkeypatch):
    client, *_ = env
    monkeypatch.setenv("APP_MODE", "production")
    assert client.get("/api/demo/tour").status_code == 404 and client.post("/api/demo/tour/map").status_code == 404


def test_a_story_is_an_order_written_once_with_the_source(env):
    client, table, actor, prs = env
    default = [s["id"] for s in client.get("/api/demo/tour").json()["steps"]]
    assert default == ["map", "unmanaged", "import_pr", "decoys", "trip", "response", "run_it"]

    # the ad about drift: the IaC tab first, every step still there, the source kept
    tour = client.post("/api/demo/tour/start", json={"story": "drift", "src": "gads", "gclid": "Cj0K" * 80, "referrer": "https://beamreach.ai/compass/pilot.html"}).json()
    assert [s["id"] for s in tour["steps"]][:3] == ["unmanaged", "import_pr", "map"] and tour["total"] == 7
    assert tour["story"] == "drift" and tour["current"] == "unmanaged"
    assert tour["source"]["src"] == "gads" and len(tour["source"]["gclid"]) == 200 and tour["source"]["referrer"].endswith("pilot.html")

    # a second landing, on another story and from elsewhere, changes nothing: the tour is under way
    again = client.post("/api/demo/tour/start", json={"story": "honeytoken", "src": "reddit"}).json()
    assert again["story"] == "drift" and again["source"]["src"] == "gads"
    row = demo.tour_row(table, PK, "visitor-1")
    assert row["story"] == "drift" and row["landed_at"] and row["started_at"]

    # the steps keep the story's order while they complete
    client.post("/api/demo/tour/unmanaged")
    tour = client.get("/api/demo/tour").json()
    assert tour["current"] == "import_pr" and [s["id"] for s in tour["steps"]][0] == "unmanaged"

    # the honeytoken story for another visitor; an unknown story is the default order with no story
    actor["user_id"] = "visitor-2"
    tour = client.post("/api/demo/tour/start", json={"story": "honeytoken"}).json()
    assert [s["id"] for s in tour["steps"]][:3] == ["decoys", "trip", "response"] and tour["story"] == "honeytoken"
    actor["user_id"] = "visitor-3"
    tour = client.post("/api/demo/tour/start", json={"story": "whatever", "src": "site"}).json()
    assert [s["id"] for s in tour["steps"]] == default and tour["story"] is None and tour["source"] == {"src": "site"}
    # a landing with nothing on it is still a landing
    actor["user_id"] = "visitor-4"
    assert client.post("/api/demo/tour/start").status_code == 200
    assert demo.tour_row(table, PK, "visitor-4")["landed_at"]


def test_the_pilot_line_shows_on_pull_request_steps_when_the_link_is_set(env, monkeypatch):
    client, table, actor, prs = env
    tour = client.get("/api/demo/tour").json()
    assert tour["pilot"] is None and "pilot" not in tour["steps"][2]["detail"]
    config = {**CONFIG, "demo": {**CONFIG["demo"], "links": {"pilot": "https://beamreach.ai/compass/pilot.html"}}}
    monkeypatch.setattr(demo_routes, "get_workspace_config", lambda request: dict(config))
    tour = client.get("/api/demo/tour").json()
    assert tour["pilot"] == {"url": "https://beamreach.ai/compass/pilot.html", "note": demo.DEFAULTS["tour"]["pilot_note"]}
    by_id = {s["id"]: s for s in tour["steps"]}
    assert by_id["import_pr"]["detail"]["pilot"]["url"].endswith("pilot.html") and "pilot" not in by_id["map"]["detail"]
    assert by_id["run_it"]["detail"]["links"]["pilot"].endswith("pilot.html")


def test_a_product_step_joins_the_tour_and_the_story_that_names_it(env):
    client, table, actor, prs = env
    seen: List[Dict[str, Any]] = []

    def cost_fact(ctx: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(ctx)
        mine = [p for p in ctx["pull_requests"] if (p.get("origin") or {}).get("command") == "solve" and demo.same_visitor((p.get("origin") or {}).get("user_id"), ctx["user_id"])]
        return {"done": bool(mine), "detail": {"href": "/workspace?tab=finops", "pr_url": mine[0]["pr_url"] if mine else None}}

    demo.register_tour_step({"id": "cost_fix", "kind": "api", "pr": True, "title": "Fix a cost finding", "why": "…", "done_when": "A cost-fix pull request by you exists."}, cost_fact)
    try:
        # the cost story starts on it; the default order puts it last; the count says 8
        tour = client.post("/api/demo/tour/start", json={"story": "cost", "src": "gads"}).json()
        assert [s["id"] for s in tour["steps"]][:2] == ["cost_fix", "unmanaged"] and tour["total"] == 8 and tour["current"] == "cost_fix"
        assert tour["steps"][0]["detail"]["href"] == "/workspace?tab=finops" and tour["steps"][0]["pr"] is True
        assert seen and seen[-1]["user_id"] == "visitor-1" and seen[-1]["pk"] == PK
        actor["user_id"] = "visitor-2"
        assert [s["id"] for s in client.get("/api/demo/tour").json()["steps"]][-1] == "cost_fix"
        actor["user_id"] = "visitor-1"

        # the fact decides: another visitor's fix is not mine; mine completes the step; skip works on it
        prs.append({"repo": "acme/infra", "number": 3, "pr_url": "https://github.com/acme/infra/pull/3", "origin": {"captain": "finops_engine", "command": "solve", "user_id": "visitor-2"}})
        assert not client.get("/api/demo/tour").json()["steps"][0]["done"]
        prs.append({"repo": "acme/infra", "number": 4, "pr_url": "https://github.com/acme/infra/pull/4", "origin": {"captain": "finops_engine", "command": "solve", "user_id": "visitor-1"}})
        tour = client.get("/api/demo/tour").json()
        assert tour["steps"][0]["done"] and tour["steps"][0]["detail"]["pr_url"].endswith("/4") and tour["current"] == "unmanaged"
        assert "cost_fix" in _ids(client.post("/api/demo/tour/cost_fix/skip").json(), "skipped")

        # a fact that fails is a step not done, not a tour that fails
        demo.register_tour_step({"id": "flaky", "kind": "api", "title": "Flaky", "why": "", "done_when": ""}, lambda ctx: 1 / 0)
        tour = client.get("/api/demo/tour").json()
        assert tour["total"] == 9 and not {s["id"]: s for s in tour["steps"]}["flaky"]["done"]
    finally:
        demo.unregister_tour_step("cost_fix")
        demo.unregister_tour_step("flaky")

    # unregistered: the cost story falls back to the drift order, the row still says cost
    tour = client.get("/api/demo/tour").json()
    assert tour["total"] == 7 and [s["id"] for s in tour["steps"]][:2] == ["unmanaged", "import_pr"] and tour["story"] == "cost"
    assert client.post("/api/demo/tour/cost_fix").status_code == 404


def test_exits_are_events_and_the_funnel_counts_them_for_the_operator(env, monkeypatch):
    from compass_api import demo_guard

    client, table, actor, prs = env
    # three visitors from two ads, one direct
    client.post("/api/demo/tour/start", json={"story": "drift", "src": "gads", "gclid": "x"})
    client.post("/api/demo/tour/unmanaged")
    prs.append({"repo": "acme/infra", "number": 8, "pr_url": "https://github.com/acme/infra/pull/8", "origin": {"captain": "iac", "command": "import", "user_id": "visitor-1"}})
    assert client.post("/api/demo/tour/event", json={"event": "pilot_click", "href": "https://beamreach.ai/compass/pilot.html?src=demo"}).json() == {"ok": True}
    assert client.post("/api/demo/tour/event", json={"event": "nope"}).status_code == 404
    row = demo.tour_row(table, PK, "visitor-1")
    assert [e["event"] for e in row["events"]] == ["pilot_click"] and row["events"][0]["href"].endswith("src=demo") and row["events"][0]["at"]
    actor["user_id"] = "visitor-2"
    client.post("/api/demo/tour/start", json={"story": "drift", "src": "gads"})
    client.post("/api/demo/tour/event", json={"event": "install_click"})
    actor["user_id"] = "visitor-3"
    client.post("/api/demo/tour/start", json={"story": "honeytoken", "src": "reddit"})
    client.post("/api/demo/tour/decoys")
    actor["user_id"] = "visitor-4"
    client.get("/api/demo/tour")  # looked, never started: not a visitor in the funnel
    prs.append({"repo": "acme/infra", "number": 1, "url": "https://github.com/acme/infra/pull/1", "origin": {"command": "seed_pr", "seeded": True}})

    # a visitor is not the operator
    monkeypatch.setattr(demo_guard, "is_demo_admin", lambda request, config=None: False)
    assert client.get("/api/demo/funnel").status_code == 403
    monkeypatch.setattr(demo_guard, "is_demo_admin", lambda request, config=None: True)
    funnel = client.get("/api/demo/funnel?days=7").json()
    assert funnel["total"]["visitors"] == 3 and funnel["total"]["pr_opened"] == 1 and funnel["total"]["pilot_click"] == 1 and funnel["total"]["install_click"] == 1
    assert funnel["total"]["steps"] == {"unmanaged": 1, "import_pr": 1, "decoys": 1} and funnel["steps"][0] == "map"
    gads = next(r for r in funnel["by_source"] if r["src"] == "gads")
    assert gads["story"] == "drift" and gads["visitors"] == 2 and gads["pr_opened"] == 1 and gads["pilot_click"] == 1 and gads["steps"] == {"unmanaged": 1, "import_pr": 1}
    reddit = next(r for r in funnel["by_source"] if r["src"] == "reddit")
    assert reddit["story"] == "honeytoken" and reddit["visitors"] == 1 and reddit["steps"] == {"decoys": 1} and reddit["pr_opened"] == 0
    assert [s["story"] for s in funnel["by_story"]] == ["drift", "honeytoken"] and funnel["by_story"][0]["visitors"] == 2
    assert funnel["by_source"][0]["src"] == "gads", "most visitors first"
    # nothing before the window
    table.update_item(Key={"pk": PK, "sk": demo.tour_row(table, PK, "visitor-3")["sk"]}, UpdateExpression="SET landed_at = :t", ExpressionAttributeValues={":t": 1})
    assert client.get("/api/demo/funnel?days=7").json()["total"]["visitors"] == 2
    # the CLI's line
    from compass_core import demo_reset

    monkeypatch.setattr(demo_reset, "PRRegistry", type("R", (), {"from_config": staticmethod(lambda config, workspace_id: type("L", (), {"list": lambda self, limit: list(prs)})())}))
    monkeypatch.setattr(demo_reset, "dynamo_table", lambda name: table)
    out = demo_reset.funnel(CONFIG, workspace_id=WS, days=7)
    assert out["summary"].splitlines()[1].startswith("gads") and out["summary"].splitlines()[-1].startswith("total") and " 2 " in out["summary"].splitlines()[-1]
