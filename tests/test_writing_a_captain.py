"""The example captain in examples/captain-hello goes through everything a real
one does: discovery by reference, its command, its route, its tab in the
bootstrap, its config form, and the toggle (docs/writing_a_captain.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compass_core import captains, dispatch, queue
from compass_core import config as core_config

HELLO = "captains.hello.manifest:MANIFEST"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "captain-hello"


@pytest.fixture
def hello_app(tmp_path, monkeypatch):
    # the example as if pip-installed: its directory on sys.path joins the captains namespace
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.setenv("COMPASS_CAPTAINS", HELLO)
    captains.reset_for_tests()
    cfg = {"workspace_id": "local", "aws": {"auth_mode": "local_iam", "region": "us-east-1"}, "github": {},
           "dynamodb_config": {"workspace_id": "local", "items": "compass_items_local"}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD", "READ_ONLY"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr(core_config, "_saver", None)
    from compass_api import extensions
    from compass_api import workspace as api_workspace

    for name in ("_bootstrap_sections", "_status_sections"):
        monkeypatch.setattr(extensions, name, {})
    for name in ("_bootstrap_hooks", "_aws_saved_hooks", "_job_restart_guards"):
        monkeypatch.setattr(extensions, name, [])
    monkeypatch.setattr(api_workspace, "_loader", api_workspace._open_loader)
    monkeypatch.setattr(api_workspace, "_saver", api_workspace._open_saver)
    monkeypatch.setattr("compass_api.routes._infra_scan.current_infra_scan_summary", lambda *a, **k: {"scanned": True})
    monkeypatch.setattr(dispatch, "_registry", {})
    previous = queue._factory
    from compass_api.app import create_app

    client = TestClient(create_app())
    dispatch.use_inline_queue()  # the test is about the captain: jobs run in the request, whatever earlier tests left installed
    yield client, path
    queue.set_job_queue(previous)
    captains.reset_for_tests()


def test_the_toy_captain_is_a_whole_captain(hello_app):
    client, path = hello_app
    assert "hello" in captains.discover(), "found through COMPASS_CAPTAINS"
    assert captains.manifest("hello").errors() == []

    boot = client.get("/api/settings/bootstrap").json()
    assert "hello" in boot["app"]["enabled_tabs"]
    assert any(t["key"] == "hello" and t["group"] == "platform" for t in boot["app"]["tabs"])
    assert any(c["name"] == "hello" and c["tier"] == "open" and c["has_config"] for c in boot["captains"])

    assert client.get("/api/hello").json() == {"greeting": "hello"}
    put = client.put("/api/captains/hello/config", json={"greeting": "hi", "api_key": "s3cretvalue"})
    assert put.status_code == 200 and put.json()["config"] == {"greeting": "hi", "api_key": "…alue"}
    assert json.loads(path.read_text())["hello"]["api_key"] == "s3cretvalue"

    # the command runs in the request (no jobs table): through the open dispatcher
    captains.load_commands("hello")
    run = client.post("/api/hello/greet", json={"name": "Compass"})
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "completed" and run.json()["result_summary"]["greeting"] == "hi, Compass"

    # and without any route of its own: the generic job start the Captains page uses
    assert client.get("/api/captains/hello").json()["commands"] == {"greet": "Say hello to name= with the configured greeting."}
    generic = client.post("/api/jobs", json={"captain": "hello", "command": "greet", "payload": {"name": "anyone"}})
    assert generic.status_code == 200, generic.text
    assert generic.json()["result_summary"]["greeting"] == "hi, anyone"
    assert client.post("/api/jobs", json={"captain": "hello", "command": "nope"}).status_code == 404
    assert client.post("/api/jobs", json={"captain": "nobody", "command": "x"}).status_code == 404

    off = client.post("/api/captains/hello/disable")
    assert off.status_code == 200
    assert "hello" not in client.get("/api/settings/bootstrap").json()["app"]["enabled_tabs"]
    refused = client.post("/api/hello/greet", json={"name": "x"})
    assert refused.status_code == 403 and refused.json()["code"] == "captain_disabled"
    refused = client.post("/api/jobs", json={"captain": "hello", "command": "greet"})
    assert refused.status_code == 403 and refused.json()["code"] == "captain_disabled", "the generic start is refused the same way"
    assert client.get("/api/hello").status_code == 200, "reads still answer"
    with pytest.raises(captains.CaptainUnavailable):
        dispatch.dispatch("hello", "greet", {"workspace_id": "local"})
