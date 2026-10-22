"""``GET /api/inbox`` filters and counts; snooze is a write, decline drops a parked response;
``/api/status`` carries the counts (docs/specs/one-inbox.md §3.4)."""

from __future__ import annotations

from types import SimpleNamespace

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.honeypot import incidents
from compass_api.auth.scopes import required_scope
from compass_api.routes import honeypot_incidents, honeypots
from compass_api.routes import inbox as inbox_routes
from compass_core import inbox
from compass_core.keys import workspace_pk

PK = workspace_pk("ws-a")
T0 = 1_792_670_000_000
CONFIG = {"aws": {"roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-x"}, "account_id": "123456789012", "region": "us-east-1",
                  "control": {"alert_queues": {"us-east-1": {"arn": "arn:aws:sqs:us-east-1:123456789012:q"}}}},
          "github": {"installation_id": 42}, "dynamodb_config": {"items": "items", "workspace_id": "ws-a"}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("COMPASS_UPDATE_CHECK", "off")
    inbox.reset_for_tests()
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        jobs = [{"job_id": "j1", "captain": "mapper", "command": "run", "status": "failed", "error": "boom", "updated_at": T0 + 10}]
        monkeypatch.setattr(inbox_routes._queue_client, "list_jobs", lambda **_k: list(jobs))
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
        monkeypatch.setattr("compass_core.llm._factory", lambda _config, _target: object())   # a model is configured
        app = FastAPI()

        @app.middleware("http")
        async def _ctx(request, call_next):
            request.state.workspace_ctx = SimpleNamespace(workspace_id="ws-a", org_id=None, user_id="ilia", config=CONFIG)
            return await call_next(request)

        app.include_router(inbox_routes.router)
        app.include_router(honeypot_incidents.router)
        yield TestClient(app), table, jobs
    inbox.reset_for_tests()


def _park(table):
    alert = {"alert_id": "a1", "honeytoken_id": "tok", "source_ip": "203.0.113.9", "source_task_id": "t1", "triggered_at": "2026-10-22T12:00:00Z"}
    incident, _ = incidents.attach_alert(table, PK, alert, placement={"service_arn": "arn:aws:ecs:us-east-1:1:service/prod/api"}, now_ms=T0)
    incidents.mark(table, PK, incident["incident_id"], action={"plan": [], "decisions": [], "context": {},
                                                                "pending": [{"action_id": "act-1", "action_type": "ecs_restart_task", "risk": 2, "reason": "mode is approve"}]})
    return incident["incident_id"]


def test_the_feed_lists_counts_and_filters(env):
    client, table, _ = env
    incident_id = _park(table)
    body = client.get("/api/inbox").json()
    assert [i["kind"] for i in body["items"]] == ["job.failed", "decoy.approval"]
    assert body["counts"]["total"] == 2 and body["counts"]["urgent"] == 1 and body["counts"]["by_tab"] == {"queue": 1, "honeypots": 1}
    assert client.get("/api/inbox?tab=honeypots").json()["items"][0]["subject"]["incident_id"] == incident_id
    assert [i["kind"] for i in client.get("/api/inbox?verb=fix").json()["items"]] == ["job.failed"]
    assert client.get("/api/inbox?kind=decoy.approval").json()["items"][0]["actions"][0]["confirm"] == "Runs ecs_restart_task on api (risk 2)."
    assert client.get("/api/inbox?verb=shout").status_code == 400


def test_snooze_hides_an_item_and_delete_brings_it_back(env):
    client, table, _ = env
    assert client.post("/api/inbox/job.failed:j1/snooze").json()["snoozed_by"] == "ilia"
    assert client.get("/api/inbox").json()["counts"]["total"] == 0
    shown = client.get("/api/inbox?include_snoozed=true").json()["items"]
    assert shown[0]["id"] == "job.failed:j1" and shown[0]["snooze"]["snoozed_by"] == "ilia"
    assert client.delete("/api/inbox/job.failed:j1/snooze").json() == {"snoozed": False, "item_id": "job.failed:j1"}
    assert client.get("/api/inbox").json()["counts"]["total"] == 1
    # only the kinds with a natural end can be snoozed
    assert client.post("/api/inbox/decoy.approval:x:y/snooze").status_code == 400


def test_declining_a_parked_response_removes_it_from_the_incident_and_the_feed(env):
    client, table, _ = env
    incident_id = _park(table)
    assert client.get("/api/inbox?tab=honeypots").json()["counts"]["by_tab"] == {"queue": 1, "honeypots": 1}
    r = client.post(f"/api/honeypots/incidents/{incident_id}/decline", json={"action_id": "act-1"})
    assert r.status_code == 200 and r.json()["declined_by"] == "ilia" and r.json()["pending"] == []
    assert client.get("/api/inbox?tab=honeypots").json()["items"] == []
    assert incidents.get_incident(table, PK, incident_id)["action"]["decisions"][-1]["decision"] == "declined"
    assert client.post(f"/api/honeypots/incidents/{incident_id}/decline", json={"action_id": "act-1"}).status_code == 400
    assert client.post("/api/honeypots/incidents/nope/decline", json={"action_id": "act-1"}).status_code == 404


def test_scopes_read_lists_operate_acts():
    assert required_scope("GET", "/api/inbox") == "read"
    assert required_scope("POST", "/api/inbox/job.failed:j1/snooze") == "operate"
    assert required_scope("DELETE", "/api/inbox/job.failed:j1/snooze") == "operate"
    assert required_scope("POST", "/api/honeypots/incidents/i/decline") == "operate"


def test_status_carries_the_inbox_counts_and_line(env, monkeypatch):
    import importlib

    from compass_api import extensions

    module = importlib.import_module("compass_api.routes.status")
    monkeypatch.setattr(module, "get_actor_ids", lambda _r: {"workspace_id": "ws-a", "org_id": None, "user_id": "ilia"})
    monkeypatch.setattr(module, "get_workspace_config", lambda _r: CONFIG)
    monkeypatch.setattr(module, "current_infra_scan_summary", lambda _r, **_k: {})
    monkeypatch.setattr(extensions, "_status_sections", {})
    monkeypatch.setattr(honeypots, "list_honeytokens", lambda _r: [])
    app = FastAPI()

    @app.middleware("http")
    async def _ctx(request, call_next):
        request.state.workspace_ctx = SimpleNamespace(workspace_id="ws-a", org_id=None, user_id="ilia", config=CONFIG)
        return await call_next(request)

    app.include_router(module.router)
    body = TestClient(app).get("/api/status").json()
    assert body["errors"] == {}
    assert body["inbox"]["counts"]["total"] == 1 and body["inbox"]["line"] == "Needs you: 1 (1 urgent): 1 to fix"
    assert body["inbox"]["newest"][0]["id"] == "job.failed:j1"
