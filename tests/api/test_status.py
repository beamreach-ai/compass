from __future__ import annotations

import importlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, *, jobs=None, break_honeypots=False):
    module = importlib.import_module("compass_api.routes.status")
    from compass_api import extensions
    from compass_core import registry

    config = {"aws": {"roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-k3f9a2"}, "account_id": "123456789012"},
              "github": {"installation_id": 42, "account_login": "acme"}}
    registry.reset_memory_for_tests()
    monkeypatch.setattr(module, "get_actor_ids", lambda _r: {"workspace_id": "ws-1", "org_id": None, "user_id": "u"})
    monkeypatch.setattr(module, "get_workspace_config", lambda _r: config)
    monkeypatch.setattr(module, "current_infra_scan_summary", lambda _r, **_k: {"scanned": True, "status": "completed"})
    # billing and findings are the product's sections (compass_api.extensions); an open install has none
    monkeypatch.setattr(extensions, "_status_sections", {})
    monkeypatch.setattr(module._queue_client, "list_jobs", lambda **_k: jobs or [])
    monkeypatch.delenv("WORKSPACES_TABLE", raising=False)
    monkeypatch.delenv("WORKSPACE_IDENTITY_MODE", raising=False)

    import compass_api.routes.honeypots as honeypots

    def list_honeytokens(_request):
        if break_honeypots:
            raise RuntimeError("dynamo down")
        return [{"status": "armed", "active_alert_count": 2}, {"status": "pending_pr", "active_alert_count": 0}]

    monkeypatch.setattr(honeypots, "list_honeytokens", list_honeytokens)

    app = FastAPI()

    @app.middleware("http")
    async def _ctx(request, call_next):
        request.state.workspace_ctx = SimpleNamespace(workspace_id="ws-1", org_id=None, user_id="u", config=config)
        return await call_next(request)

    app.include_router(module.router)
    return TestClient(app)


def test_status_aggregates_sections(monkeypatch):
    jobs = [
        {"job_id": "j1", "captain": "mapper", "command": "run", "status": "completed", "updated_at": 100, "created_at": 90},
        {"job_id": "j2", "captain": "iac", "command": "import", "status": "running", "created_at": 95},
        {"job_id": "j3", "captain": "honeypot", "command": "plant", "status": "completed", "created_at": 96,
         "result_summary": {"pr_url": "https://github.com/acme/infra/pull/3"}, "payload": {}, "events": []},
        {"job_id": "j4", "captain": "prs", "command": "sync", "status": "failed", "error": "nope", "created_at": 97},
    ]
    body = _client(monkeypatch, jobs=jobs).get("/api/status").json()
    assert body["api_version"] and body["workspace"]["workspace_id"] == "ws-1"
    assert body["target"]["aws_connected"] is True and body["target"]["github_connected"] is True
    assert body["infra_scan"]["scanned"] is True
    assert body["jobs"]["active_count"] == 1 and body["jobs"]["last_completed"]["mapper.run"] == 100
    assert body["jobs"]["recent_failures"][0]["job_id"] == "j4"
    assert body["prs"]["opened"] == 1 and body["prs"]["recent"][0]["type"] == "honeypot_plant"
    assert body["honeypots"] == {"total": 2, "by_status": {"armed": 1, "pending_pr": 1}, "active_alerts": 2}
    # the product's sections are absent, not empty
    assert "findings" not in body and "billing" not in body
    assert body["errors"] == {}


def test_status_degrades_when_a_section_fails(monkeypatch):
    body = _client(monkeypatch, break_honeypots=True).get("/api/status").json()
    assert body["honeypots"] == {}
    assert "dynamo down" in body["errors"]["honeypots"]
    assert body["target"]["aws_connected"] is True
    assert body["jobs"]["active_count"] == 0 and body["prs"]["opened"] == 0
    assert set(body["errors"]) == {"honeypots"}
