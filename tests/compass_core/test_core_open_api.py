"""The open API app and the job hand-off between the API and `compass serve`."""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from compass_core import config as core_config
from compass_core import dispatch, queue


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BEAMREACH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CAPTAIN_JOBS_TABLE", "")
    local_state = importlib.import_module("compass_core.local_state")
    jobs = importlib.import_module("compass_core.jobs")
    state = local_state.LocalStateStore(path=str(tmp_path / "state.json"))
    monkeypatch.setattr(local_state, "local_state_store", state)
    monkeypatch.setattr(jobs, "local_state_store", state)
    return jobs.CaptainJobStore()


def test_store_queue_writes_queued_jobs_and_dedupes_sessions(local_store):
    q = queue.StoreQueue(local_store)
    first = q.enqueue(captain="mapper", command="run", payload={"workspace_id": "ws"}, session_id="scan", enforce_single_active_session=True)
    assert first["status"] == "queued"
    again = q.enqueue(captain="mapper", command="run", payload={"workspace_id": "ws"}, session_id="scan", enforce_single_active_session=True)
    assert again["job_id"] == first["job_id"], "an active job for the session is returned, not duplicated"
    assert [j["job_id"] for j in q.list_jobs(workspace_id="ws")] == [first["job_id"]]


def test_serve_runs_what_the_api_queued(local_store, monkeypatch):
    from compass_core import serve

    monkeypatch.setattr(dispatch, "_registry", {})
    seen = []
    dispatch.register("x", {"go": lambda p: seen.append(p) or {"done": True}, "boom": lambda p: (_ for _ in ()).throw(RuntimeError("nope"))})
    monkeypatch.setattr("compass_core.jobs.store_for", lambda config: local_store)
    q = queue.StoreQueue(local_store)
    ok = q.enqueue(captain="x", command="go", payload={"workspace_id": "ws", "n": 1})
    bad = q.enqueue(captain="x", command="boom", payload={"workspace_id": "ws"})

    server = serve.Server("ws", {"workspace_id": "ws"}, prs_tick=0)
    assert server._run_queued_jobs() == 2
    assert seen and seen[0]["n"] == 1
    assert local_store.get_job(ok["job_id"], workspace_id="ws")["status"] == "completed"
    failed = local_store.get_job(bad["job_id"], workspace_id="ws")
    assert failed["status"] == "failed" and "nope" in failed["error"]
    assert server._run_queued_jobs() == 0, "nothing is run twice"


def test_open_app_serves_health_bootstrap_and_hides_closed_tabs(tmp_path, monkeypatch):
    cfg = {
        "workspace_id": "local",
        "aws": {"auth_mode": "local_iam", "region": "us-east-1"},
        "github": {},
        "dynamodb_config": {"workspace_id": "local", "items": "compass_items_local"},
    }
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD"):
        # set-then-delete records the original state, so what create_app() sets is undone afterwards
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    from compass_api import extensions
    from compass_api import workspace as api_workspace

    # An open install has none of the product's sections or hooks (other tests in this
    # process register them); empty registries for this test, restored afterwards.
    for name in ("_bootstrap_sections", "_status_sections"):
        monkeypatch.setattr(extensions, name, {})
    for name in ("_bootstrap_hooks", "_aws_saved_hooks", "_job_restart_guards"):
        monkeypatch.setattr(extensions, name, [])

    monkeypatch.setattr(api_workspace, "_loader", api_workspace._open_loader)
    monkeypatch.setattr(api_workspace, "_saver", api_workspace._open_saver)
    monkeypatch.setattr("compass_api.routes._infra_scan.current_infra_scan_summary", lambda *a, **k: {"scanned": False})
    previous = queue._factory
    try:
        from compass_api.app import create_app

        client = TestClient(create_app())
        assert client.get("/health").json() == {"status": "ok"}
        boot = client.get("/api/settings/bootstrap")
        assert boot.status_code == 200, boot.text
        body = boot.json()
        assert body["workspace"]["workspace_id"] == "local"
        tabs = body["app"]["enabled_tabs"]
        assert {"honeypots", "maps", "iac", "prs", "queue", "settings"} <= set(tabs)
        assert not {"finops", "audits", "compliance", "policies"} & set(tabs)
        assert body["billing"] is None and body["triage"] is None
    finally:
        queue.set_job_queue(previous)


def test_open_app_takes_the_sign_in_from_the_config_file(tmp_path, monkeypatch):
    cfg = {"workspace_id": "local", "dashboard": {"username": "admin", "password": "s3cret-pass"},
           "dynamodb_config": {"workspace_id": "local", "items": "compass_items_local"}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    from compass_api import workspace as api_workspace

    monkeypatch.setattr(api_workspace, "_loader", api_workspace._open_loader)
    monkeypatch.setattr(api_workspace, "_saver", api_workspace._open_saver)
    previous = queue._factory
    try:
        from compass_api.app import create_app

        client = TestClient(create_app())
        assert client.get("/api/settings/bootstrap").status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}).status_code == 200
    finally:
        queue.set_job_queue(previous)


def test_local_iam_account_is_looked_up_once_and_kept(monkeypatch):
    from compass_api.routes import settings as open_settings

    class _Sts:
        calls = 0

        def get_caller_identity(self):
            _Sts.calls += 1
            return {"Account": "123456789012"}

    class _Session:
        def client(self, name):
            assert name == "sts"
            return _Sts()

    monkeypatch.setattr(open_settings, "build_target_session", lambda *a, **k: _Session())
    cfg = {"aws": {"auth_mode": "local_iam", "region": "us-east-1"}}
    assert open_settings._resolve_local_account(cfg) is True
    assert cfg["aws"]["account_id"] == "123456789012"
    assert open_settings._resolve_local_account(cfg) is False and _Sts.calls == 1, "already known: no second lookup"
    assert open_settings._resolve_local_account({"aws": {"assume_role": {"account_id": "1"}}}) is False
