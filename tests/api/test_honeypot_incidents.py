"""HP-07 routes: list, detail, close, report."""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.honeypot import incidents as inc
from compass_api.routes import honeypot_incidents, honeypots
from compass_core.keys import honeypot_alert_sort_key, honeypot_audit_sort_key, workspace_pk

PK = workspace_pk("ws-a")
T0 = 1_790_000_000_000


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
        app = FastAPI()
        app.include_router(honeypot_incidents.router)
        yield TestClient(app), table


def _make(table, n, task):
    a = {"alert_id": f"a{n}", "honeytoken_id": "tok", "source_ip": "10.0.0.1", "source_task_id": task, "triggered_at": f"2026-10-06T12:00:0{n}Z"}
    incident, _ = inc.attach_alert(table, PK, a, placement={"service_arn": "svc"}, now_ms=T0 + n * 1000)
    table.put_item(Item={**a, "pk": PK, "sk": honeypot_alert_sort_key("tok", a["alert_id"]), "incident_id": incident["incident_id"]})
    return incident


def test_list_newest_first_with_repeat_link(env):
    client, table = env
    first = _make(table, 1, "t")
    inc.mark(table, PK, first["incident_id"], action_verified_at="2026-10-06T12:05:00Z")
    second = _make(table, 2, "t")
    rows = client.get("/api/honeypots/incidents").json()
    assert [r["incident_id"] for r in rows] == [second["incident_id"], first["incident_id"]]
    assert rows[0]["related_incident_id"] == first["incident_id"] and rows[0]["repeat_count"] == 2
    assert rows[1]["status"] == "contained" and rows[1]["alert_count"] == 1
    assert len(client.get("/api/honeypots/incidents?limit=1").json()) == 1


def test_detail_includes_alerts_and_only_matching_audits(env):
    client, table = env
    incident = _make(table, 1, "t")
    other = _make(table, 2, "other")
    for aid, iid in (("100-a", incident["incident_id"]), ("200-b", other["incident_id"])):
        table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key(aid), "audit_id": aid, "incident_id": iid, "action_type": "ecs_restart_task", "status": "verified", "verify": {"ok": True}})
    body = client.get(f"/api/honeypots/incidents/{incident['incident_id']}").json()
    assert body["incident"]["incident_id"] == incident["incident_id"]
    assert [a["alert_id"] for a in body["alerts"]] == ["a1"]
    assert [a["audit_id"] for a in body["audits"]] == ["100-a"]
    assert client.get("/api/honeypots/incidents/nope").status_code == 404


def test_close_is_idempotent_and_stops_joining(env):
    client, table = env
    incident = _make(table, 1, "t")
    r = client.post(f"/api/honeypots/incidents/{incident['incident_id']}/close")
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["closed_at"]
    closed_at = r.json()["closed_at"]
    assert client.post(f"/api/honeypots/incidents/{incident['incident_id']}/close").json()["closed_at"] == closed_at
    assert client.post("/api/honeypots/incidents/nope/close").status_code == 404
    again = _make(table, 3, "t")
    assert again["incident_id"] != incident["incident_id"]


def test_report_has_markdown_and_null_summary(env):
    client, table = env
    incident = _make(table, 1, "t")
    table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key("1-a"), "audit_id": "1-a", "incident_id": incident["incident_id"], "action_type": "ecs_restart_task", "status": "verified", "started_at": "s", "verify": {"ok": True}})
    body = client.get(f"/api/honeypots/incidents/{incident['incident_id']}/report").json()
    assert body["summary"] is None
    md = body["markdown"]
    assert incident["incident_id"] in md and "## Timeline" in md and "First event" in md
    assert "Summary pending." in md
    assert "`ecs_restart_task`: verified" in md
    assert set(body) == {"incident", "alerts", "audits", "summary", "markdown"}


def test_summarize_caches_and_report_uses_it(env, monkeypatch):
    from captains.honeypot import summary as summary_mod

    client, table = env
    incident = _make(table, 1, "t")
    calls = []

    def fake(prompt, facts_json):
        calls.append(1)
        return '```json\n{"paragraph": "Decoy tripped.", "bullets": ["a", "b", "c"]}\n```'

    monkeypatch.setattr(summary_mod, "INVOKE", fake)
    url = f"/api/honeypots/incidents/{incident['incident_id']}"
    r = client.post(url + "/summarize")
    assert r.status_code == 200 and r.json()["paragraph"] == "Decoy tripped."
    assert client.post(url + "/summarize").status_code == 200 and len(calls) == 1
    body = client.get(url + "/report").json()
    assert body["summary"]["bullets"] == ["a", "b", "c"] and "Decoy tripped." in body["markdown"]
    assert client.post("/api/honeypots/incidents/nope/summarize").status_code == 404


def test_summarize_timeout_is_504(env, monkeypatch):
    import time

    from captains.honeypot import summary as summary_mod

    client, table = env
    incident = _make(table, 1, "t")
    monkeypatch.setattr(summary_mod, "MODEL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(summary_mod, "INVOKE", lambda p, f: time.sleep(1) or "{}")
    r = client.post(f"/api/honeypots/incidents/{incident['incident_id']}/summarize")
    assert r.status_code == 504 and "timed out" in r.json()["detail"]


def test_summarize_bad_model_output_is_502(env, monkeypatch):
    from captains.honeypot import summary as summary_mod

    client, table = env
    incident = _make(table, 1, "t")
    monkeypatch.setattr(summary_mod, "INVOKE", lambda p, f: "nonsense")
    assert client.post(f"/api/honeypots/incidents/{incident['incident_id']}/summarize").status_code == 502
