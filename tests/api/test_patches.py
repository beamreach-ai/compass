"""``/api/patches``: the findings with their summary and the feed's status; approve and ignore from
a person; the feeds and the match as jobs; the inbox items the captain contributes
(docs/specs/patching-captain.md §3.8)."""

from __future__ import annotations

from types import SimpleNamespace

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.patch import inbox as patch_inbox
from captains.patch import scheduler
from captains.patch.findings import FindingStore
from captains.patch.store import AdvisoryStore
from compass_api.routes import patches as routes
from compass_core import inbox
from compass_core.keys import workspace_pk
from tests.captains.patch.test_match import php2shell, php_shop

PK = workspace_pk("ws-a")
CONFIG = {"dynamodb_config": {"items": "items", "workspace_id": "ws-a"}, "patch": {"act_on": {"cvss_min": 7.0}}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    inbox.reset_for_tests()
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        advisories, findings = AdvisoryStore(table, PK), FindingStore(table, PK)
        advisories.put(php2shell())
        nodes, edges = php_shop()
        from captains.patch import inventory as _inventory

        report = scheduler.tick(CONFIG, workspace_id="ws-a", inventory=_inventory.collect(nodes, edges), advisories=advisories, findings=findings, now_ms=1000)
        enqueued = []
        monkeypatch.setattr(routes._queue_client, "enqueue", lambda **k: enqueued.append(k) or {"job_id": "j-1", "status": "queued"})
        app = FastAPI()

        @app.middleware("http")
        async def _ctx(request, call_next):
            request.state.workspace_ctx = SimpleNamespace(workspace_id="ws-a", org_id=None, user_id="ilia", config=CONFIG)
            return await call_next(request)

        app.include_router(routes.router)
        yield TestClient(app), table, findings, report, enqueued
    inbox.reset_for_tests()


def test_the_tab_gets_the_findings_the_summary_the_tick_and_the_feed_in_one_call(env):
    client, _, _, report, _ = env
    assert report["matched"] == 6 and report["changes"]["opened"] == 6 and report["coverage"]["line"] == "1 of 1 images resolved"
    body = client.get("/api/patches").json()
    assert body["summary"]["open"] == 6 and body["summary"]["exposed"] == 3 and body["summary"]["pr_now"] == 2
    assert [f["node_name"] for f in body["findings"]][:1] == ["web/Dockerfile"], "exposed and most severe first"
    assert body["tick"]["matched"] == 6 and body["coverage"]["images"] == 1 and body["feed"]["advisories"] == 1
    assert body["settings"]["act_on"]["cvss_min"] == 7.0
    exposed = client.get("/api/patches/findings", params={"exposed": "true"}).json()
    assert {f["node_id"] for f in exposed} == {"f-web", "fn-0", "fn-1"}
    assert [f["repo"] for f in client.get("/api/patches/findings", params={"repo": "acme/legacy"}).json()] == ["acme/legacy"]
    key = exposed[0]["key"]
    detail = client.get(f"/api/patches/findings/{key}").json()
    assert detail["finding"]["key"] == key and detail["advisory"]["id"] == "CVE-2026-109618"
    assert client.get("/api/patches/findings/nope").status_code == 404


def test_approve_and_ignore_change_the_state_and_the_inbox_item_leaves(env):
    client, table, findings, _, _ = env
    ctx = inbox.Context(config=CONFIG, workspace_id="ws-a", pk=PK, table=table, now_ms=5000)
    items = patch_inbox.items(ctx)
    by_id = {i.id: i for i in items}
    assert set(by_id) == {"patch.approve:acme/functions", "patch.approve:workload:fn-0", "patch.approve:workload:fn-1", "patch.approve:workload:fn-2"}
    functions = by_id["patch.approve:acme/functions"]
    assert functions.title == "Open the pull request for 1 finding in acme/functions" and functions.verb == "approve" and functions.tab == "patches"
    assert functions.actions[0].path == "/api/patches/findings/approve" and functions.actions[0].body["keys"] and "approved" in functions.actions[0].confirm
    assert by_id["patch.approve:workload:fn-0"].severity == "urgent" and by_id["patch.approve:workload:fn-2"].severity == "normal"
    assert by_id["patch.approve:workload:fn-0"].detail["findings"][0]["advisory_id"] == "CVE-2026-109618"
    # the whole feed carries them through the manifest's inbox hook
    feed = inbox.items(ctx, fresh=True)
    assert feed["counts"]["by_tab"]["patches"] == 4
    # approve one repository's findings
    r = client.post("/api/patches/findings/approve", json={"repo": "acme/functions"})
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert "patch.approve:acme/functions" not in {i.id for i in patch_inbox.items(ctx)}
    approved = [f for f in findings.list() if f["state"] == "approved"]
    assert approved and approved[0]["approved_by"] == "ilia" and approved[0]["verdict"] == "pr"
    # ignore another for 30 days, then reopen it
    key = by_id["patch.approve:workload:fn-2"].subject["keys"][0]
    r = client.post("/api/patches/findings/ignore", json={"keys": [key], "days": 30, "reason": "internal only"})
    assert r.status_code == 200 and r.json()["ignored"] == 1 and r.json()["until"] >= "2026-11-20"
    assert findings.get(key)["state"] == "ignored" and findings.get(key)["ignored"]["reason"] == "internal only"
    assert client.post(f"/api/patches/findings/{key}/reopen").json()["state"] == "open"
    assert client.post(f"/api/patches/findings/{key}/reopen").status_code == 409
    assert client.post("/api/patches/findings/approve", json={}).status_code == 400
    assert client.get("/api/patches", params={"state": "ignored"}).json()["findings"] == []


def test_the_feeds_and_the_match_run_as_jobs(env):
    client, _, _, _, enqueued = env
    assert client.post("/api/patches/feed/run").status_code == 202 and client.post("/api/patches/tick").status_code == 202
    assert [(e["captain"], e["command"]) for e in enqueued] == [("patch", "feed_run"), ("patch", "tick")]
    assert enqueued[0]["payload"] == {"workspace_id": "ws-a", "trigger": "api"}


def test_scopes():
    from compass_api.auth.scopes import required_scope

    assert required_scope("GET", "/api/patches") == "read"
    assert required_scope("POST", "/api/patches/findings/approve") == "operate"
    assert required_scope("POST", "/api/patches/tick") == "operate"
