"""The tour (docs/demo.md): seven steps per visitor. Client-only steps need the post; the
others are read from the world: a pull request by this visitor, their trip (or the one in
force they were shown), the verified response on that incident. Skip works on each; a
second visitor starts at step 1; outside demo mode the routes are not there."""

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
