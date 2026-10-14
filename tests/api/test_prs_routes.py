from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compass_core import registry as pr_registry


@pytest.fixture
def client(monkeypatch):
    pr_registry.reset_memory_for_tests()
    monkeypatch.delenv("READ_ONLY", raising=False)
    module = importlib.import_module("compass_api.routes.prs")
    config = {"workspace_id": "ws-1", "prs": {"watcher": {"mode": "autopilot"}}}
    monkeypatch.setattr(module, "get_actor_ids", lambda _r: {"workspace_id": "ws-1", "org_id": None, "user_id": "u1"})
    monkeypatch.setattr(module, "get_workspace_config", lambda _r: config)
    monkeypatch.setattr(module, "save_config", lambda _r, _c: None)
    monkeypatch.setattr(module, "ensure_not_read_only", lambda *a, **k: None)
    monkeypatch.setattr(module._queue_client, "list_jobs", lambda **_k: [
        {"job_id": "j-old", "captain": "iac", "command": "import", "status": "completed", "result_summary": {"pr_url": "https://github.com/acme/infra/pull/3", "resource_count": 1}, "payload": {"repo": "acme/infra"}, "events": [], "created_at": 1, "updated_at": 2},
    ])
    enqueued = []
    monkeypatch.setattr(module._queue_client, "enqueue", lambda **k: enqueued.append(k) or {"job_id": "j-new", "status": "queued"})
    registry = pr_registry.PRRegistry(None, "ws-1")
    registry.record(url="https://github.com/acme/infra/pull/7", pr_type="security_fix", origin={"captain": "engine", "finding_id": "f1"}, intent={"goal": "Block public access", "risk": 2, "scope": {"files": ["a.tf"]}, "invariants": [{"kind": "files_differ_from_base", "files": ["a.tf"]}]}, branch="beamreach/f1", base_branch="main", title="Fix")
    app = FastAPI()
    app.include_router(module.router)
    yield TestClient(app), enqueued, registry, config
    pr_registry.reset_memory_for_tests()


def test_list_merges_tracked_and_job_rows(client):
    tc, _, _, _ = client
    rows = tc.get("/api/prs").json()
    tracked = [r for r in rows if r["tracked"]]
    untracked = [r for r in rows if not r["tracked"]]
    assert len(tracked) == 1 and tracked[0]["number"] == 7 and tracked[0]["intent"]["invariants_text"]
    assert "contract" not in tracked[0]["intent"] and "revisions" not in tracked[0]
    assert len(untracked) == 1 and untracked[0]["pr_url"].endswith("/pull/3")
    assert [r["number"] for r in tc.get("/api/prs?state=open").json()] == [7]


def test_detail_and_404(client):
    tc, _, _, _ = client
    detail = tc.get("/api/prs/acme/infra/7").json()
    assert detail["revisions"] == [] and detail["intent"]["goal"] == "Block public access"
    assert tc.get("/api/prs/acme/infra/99").status_code == 404


def test_address_plan_then_enqueue(client):
    tc, enqueued, _, _ = client
    plan = tc.post("/api/prs/acme/infra/7/address", json={"message": "rename the variable", "confirm": False}).json()
    assert plan["executed"] is False and plan["plan"]["decision"] == "run" and plan["plan"]["fetch_comments"] is False
    done = tc.post("/api/prs/acme/infra/7/address", json={"message": "rename the variable"}).json()
    assert done["executed"] is True and done["job_id"] == "j-new"
    assert enqueued[0]["captain"] == "prs" and enqueued[0]["payload"]["message"] == "rename the variable" and enqueued[0]["payload"]["trigger"] == "manual"
    assert enqueued[0]["session_id"] == "pr:acme/infra#7"


def test_blocked_address_is_409(client):
    tc, _, registry, _ = client
    registry.set_watch("acme/infra", 7, "paused", "paused by a person")
    assert tc.post("/api/prs/acme/infra/7/address", json={"message": "x"}).status_code == 409
    assert tc.post("/api/prs/acme/infra/7/resume").json()["watch"]["status"] == "watching"
    assert tc.post("/api/prs/acme/infra/7/pause").json()["watch"]["status"] == "paused"


def test_pending_revision_approve_and_dismiss(client):
    tc, enqueued, registry, _ = client
    assert tc.post("/api/prs/acme/infra/7/revision/approve").status_code == 409
    registry.update("acme/infra", 7, pending_revision={"trigger": "review", "feedback": {"comments": [{"id": 1, "body": "b", "kind": "change_in_scope"}]}})
    assert tc.post("/api/prs/acme/infra/7/revision/approve").json()["job_id"] == "j-new"
    assert enqueued[-1]["payload"]["approved"] is True and enqueued[-1]["payload"]["review_feedback"]["comments"][0]["id"] == 1
    assert tc.post("/api/prs/acme/infra/7/revision/dismiss").json() == {"ok": True}
    assert registry.get("acme/infra", 7)["pending_revision"] is None


def test_watcher_settings(client):
    tc, _, _, config = client
    assert tc.get("/api/prs/watcher").json()["mode"] == "autopilot"
    updated = tc.put("/api/prs/watcher", json={"mode": "approve", "risk_max": 1, "max_revisions": 2, "auto_merge": {"enabled": True}}).json()
    assert updated["mode"] == "approve" and updated["risk_max"] == 1 and updated["auto_merge"]["enabled"] is True
    assert config["prs"]["watcher"]["max_revisions"] == 2
    assert tc.put("/api/prs/watcher", json={"mode": "yolo"}).status_code == 400


def test_sync_endpoints_enqueue(client):
    tc, enqueued, _, _ = client
    assert tc.post("/api/prs/sync").json()["job_id"] == "j-new"
    assert enqueued[-1]["command"] == "sync" and enqueued[-1]["payload"]["force"] is True and enqueued[-1]["session_id"] == "prs-sync"
    assert tc.post("/api/prs/acme/infra/7/sync").json()["job_id"] == "j-new"
    assert enqueued[-1]["payload"]["number"] == 7 and enqueued[-1]["session_id"] == "pr-sync:acme/infra#7"
    assert tc.post("/api/prs/acme/infra/99/sync").status_code == 404
