"""init_config, init_tables and serve: the setup a new install goes through (connect: test_connect.py)."""

import json
import threading

import pytest

from compass_core import commands, dispatch, queue, serve
from compass_core import config as core_config


def test_init_config_writes_a_complete_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_gh_token", lambda: "ghp_test")
    monkeypatch.setattr(commands, "_gh_login", lambda token: "octocat")
    for name in ("APP_PASSWORD", "APP_USERNAME", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    out = tmp_path / "local.json"
    result = commands.init_config({"workspace": "local", "region": "eu-west-1", "github_repo": "acme/infra", "terraform_root": "envs/prod", "out": str(out)})
    cfg = json.loads(out.read_text())
    assert cfg["aws"]["region"] == "eu-west-1" and len(cfg["aws"]["install_id"]) == 6 and "auth_mode" not in cfg["aws"]
    assert result["aws_account"].startswith("not connected yet: run `compass connect"), "the profile is for connect; init_config reads no account"
    assert cfg["github"]["token"] == "ghp_test" and cfg["github"]["login"] == "octocat" and cfg["github"]["honeypot_repo"] == "acme/infra" and cfg["github"]["org"] == "acme"
    assert cfg["honeypot"]["terraform_root"] == "envs/prod"
    assert cfg["dynamodb_config"]["map"] == "compass_map_local" and cfg["dynamodb_config"]["items"] == "compass_items_local"
    assert cfg["map"] == {"store": "sqlite"}  # a new install's map is the SQLite file
    assert result["config"]["github"]["token"] == "<set>" and result["github_token"] == "from gh CLI"
    assert cfg["dashboard"]["username"] == "admin" and len(cfg["dashboard"]["password"]) >= 12
    assert result["login"]["username"] == "admin" and result["login"]["password"] == cfg["dashboard"]["password"]
    assert result["config"]["dashboard"]["password"] == "<see login>"
    with pytest.raises(FileExistsError):
        commands.init_config({"workspace": "local", "out": str(out)})


def test_serve_once_runs_one_prs_tick_and_the_alert_consumer(monkeypatch):
    monkeypatch.setattr(core_config, "_loader", lambda ws: {"workspace_id": ws, "honeypot": {"alert_queue_url": "https://sqs.us-east-1.amazonaws.com/1/q"}})
    ticks = []
    monkeypatch.setattr("captains.prs.scheduler.tick", lambda workspaces, client=None: ticks.append(list(workspaces)) or 1)

    class FakeConsumer:
        instances = []

        def __init__(self, queue_url=None, **_):
            self.queue_url = queue_url
            self.stopped = threading.Event()
            FakeConsumer.instances.append(self)

        def run(self, install_signals=True):
            self.stopped.wait(5)

        def stop(self, *_):
            self.stopped.set()

    monkeypatch.setattr("captains.honeypot.alert_consumer.AlertConsumer", FakeConsumer)
    previous = queue._factory
    try:
        result = serve.serve({"workspace_id": "ws-1", "once": True, "prs_tick": 300})
    finally:
        queue.set_job_queue(previous)
    assert ticks == [["ws-1"]] and result["ticks"] == 4, \
        "once: one tick each of the PR watcher, the grant verification, the access audit and the demo re-arm (which returns at once outside demo mode)"
    assert "honeypot:alert-consumer" in result["services"] and "prs:prs-tick" in result["services"]
    assert {"core:grant-verify", "core:access-audit"} <= set(result["services"])
    assert "mapper:map-rescan" not in result["services"], "no re-scan without an interval"
    assert FakeConsumer.instances[0].queue_url == "https://sqs.us-east-1.amazonaws.com/1/q"
    assert FakeConsumer.instances[0].stopped.is_set(), "serve stops its consumers on exit"
    assert ("honeypot", "investigate") in dispatch.commands(), "serve loads every open command table"


def test_workspace_id_comes_from_the_config_file_when_nothing_else_names_it(tmp_path, monkeypatch):
    from compass_core import workspace

    monkeypatch.delenv("DEFAULT_WORKSPACE", raising=False)
    monkeypatch.delenv("TARGET", raising=False)
    monkeypatch.delenv("COMPASS_CONFIG", raising=False)
    monkeypatch.setattr(core_config, "_loader", None)
    with pytest.raises(LookupError, match="workspace_id is required"):
        workspace.resolve({})
    path = tmp_path / "ws.json"
    path.write_text(json.dumps({"workspace_id": "from-file", "aws": {"region": "eu-west-1"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    ws = workspace.resolve({})
    assert ws.workspace_id == "from-file" and ws.config["aws"]["region"] == "eu-west-1"
    monkeypatch.setenv("DEFAULT_WORKSPACE", "explicit")
    assert workspace.default_workspace_id() == "explicit"


def test_queue_urls_fall_back_to_the_environment(monkeypatch):
    from captains.honeypot.alert_consumer import queue_urls

    monkeypatch.setenv("HONEYPOT_ALERT_QUEUE_URLS", "https://a/q1, https://a/q2")
    assert queue_urls({}) == ["https://a/q1", "https://a/q2"]
    assert queue_urls({"honeypot": {"alert_queue_urls": ["https://c/q"]}}) == ["https://c/q"]
    connected = {"aws": {"control": {"alert_queues": {"eu-west-1": {"arn": "arn:aws:sqs:eu-west-1:1:q", "url": "https://d/q"}}}},
                 "honeypot": {"alert_queue_url": "https://old/q"}}
    assert queue_urls(connected) == ["https://d/q"], "connect's queues win over the decoy captain's old keys"


def test_env_password_wins_and_no_password_is_a_warning(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "from-env")
    monkeypatch.setenv("APP_USERNAME", "ops")
    assert commands.dashboard_login({"dashboard": {"password": "file"}})["username"] == "ops"
    monkeypatch.delenv("APP_PASSWORD")
    assert commands.dashboard_login({"dashboard": {"username": "admin", "password": "file"}})["password"] == "file"
    assert "warning" in commands.dashboard_login({})


def test_a_service_starts_with_the_config_as_it_is_now_not_as_it_was_when_serve_started(monkeypatch):
    """`connect` may run after `up -d`: the alert consumer, restarted by the daemon's check,
    must see the queue it recorded without a restart of the daemon."""
    from compass_core.captains import CaptainManifest, ServiceSpec

    current = {"workspace_id": "ws-1", "honeypot": {}}
    monkeypatch.setattr(core_config, "_loader", lambda ws: dict(current))
    seen = []
    monkeypatch.setattr("compass_core.captains.load_reference", lambda ref: lambda ws, config, stop, interval=None: seen.append(config))
    manifest = CaptainManifest(name="x", label="X", summary="s", tier="open", feature="x", services=(ServiceSpec(name="svc", start="m:f"),))
    server = serve.Server("ws-1", {"workspace_id": "ws-1", "org_id": "org-1", "honeypot": {}}, prs_tick=0)
    current["honeypot"] = {"alert_queue_url": "https://sqs.us-east-1.amazonaws.com/1/q"}
    server._run_service(manifest, manifest.services[0], None, threading.Event())
    assert seen[0]["honeypot"]["alert_queue_url"] == "https://sqs.us-east-1.amazonaws.com/1/q"
    assert seen[0]["org_id"] == "org-1", "the ids the daemon resolved at start are kept"


def test_a_restart_from_the_page_stops_the_daemon_before_it_claims_another_job(tmp_path, monkeypatch):
    """The page restarts the API at once; a daemon that waited for its 30-second check claimed
    the next job with the old set of captains and failed it (found by the e2e suite)."""
    import time

    from compass_core import extensions

    monkeypatch.setenv("COMPASS_EXTENSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_SELF_RESTART", "1")
    monkeypatch.setattr(core_config, "_loader", lambda ws: {"workspace_id": ws})
    claimed = []
    server = serve.Server("ws-1", {"workspace_id": "ws-1"}, prs_tick=0, job_poll=0.05, captains_poll=60)
    monkeypatch.setattr(server, "_run_queued_jobs", lambda: claimed.append(time.time()) or 0)
    monkeypatch.setattr(server, "_reconcile_services", lambda: None)
    previous = queue._factory
    thread = threading.Thread(target=server.run)
    try:
        thread.start()
        time.sleep(0.3)
        extensions.request_restart()
        thread.join(timeout=5)
        assert not thread.is_alive() and server.restarting, "the daemon exits within a job poll, not a 60 s check"
        after = [t for t in claimed if t > float((tmp_path / "restart").read_text())]
        assert len(after) <= 1
    finally:
        server.stop()
        queue.set_job_queue(previous)
