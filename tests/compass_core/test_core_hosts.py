"""Hosts: a product on top of Compass installs its seams once, before the CLI
and the API use the open defaults (compass_core.hosts)."""

from __future__ import annotations

import json
import sys
import types

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from compass_core import config as core_config
from compass_core import hosts, queue

CALLS: list = []


def _module(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    hosts.reset_for_tests()
    CALLS.clear()
    previous = queue._factory
    from compass_api import extensions

    for name in ("_routers", "_startup", "_shutdown", "_bootstrap_sections", "_status_sections"):
        monkeypatch.setattr(extensions, name, [] if name.startswith(("_r", "_st", "_sh")) and name not in ("_status_sections",) else {})
    yield
    hosts.reset_for_tests()
    queue.set_job_queue(previous)
    sys.modules.pop("fake_host", None)


def test_each_host_runs_once_and_a_broken_one_is_recorded(monkeypatch):
    _module("fake_host", good=lambda: CALLS.append("good"), bad=lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setenv("COMPASS_HOSTS", "fake_host:good, fake_host:bad")
    outcome = hosts.install()
    assert outcome["fake_host:good"] == "ok" and "nope" in outcome["fake_host:bad"]
    hosts.install()
    assert CALLS == ["good"], "once per process"
    assert hosts.installed() == ["fake_host:good"]


def test_a_host_shapes_the_api(tmp_path, monkeypatch):
    router = APIRouter()

    @router.get("/api/hosted")
    def hosted() -> dict:
        return {"by": "host"}

    class HostQueue:
        def enqueue(self, **kw):
            return {"job_id": "h1", "status": "queued"}

    def install_host():
        from compass_api import extensions

        queue.set_job_queue(lambda: HostQueue())
        extensions.register_router(router)
        extensions.register_startup(lambda: CALLS.append("startup"))
        extensions.register_shutdown(lambda: CALLS.append("shutdown"))

    _module("fake_host", install=install_host)
    monkeypatch.setenv("COMPASS_HOSTS", "fake_host:install")
    cfg = {"workspace_id": "local", "dynamodb_config": {"workspace_id": "local", "items": "t", "jobs": "jobs_t"}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    from compass_api.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/hosted").json() == {"by": "host"}
        assert isinstance(queue.job_queue(), HostQueue), "the open default does not replace the host's queue"
        import os

        assert "DISABLED_FEATURES" not in os.environ, "with a host, the closed tabs are not hidden by default"
    assert CALLS == ["startup", "shutdown"]


def test_without_a_host_the_open_defaults_apply(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_HOSTS", raising=False)
    cfg = {"workspace_id": "local", "dynamodb_config": {"workspace_id": "local", "items": "t"}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "DEFAULT_WORKSPACE", "TARGET", "WORKSPACES_TABLE", "APP_USERNAME", "APP_PASSWORD"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    queue.set_job_queue(None)
    from compass_api.app import create_app

    create_app()
    import os

    assert os.environ["DISABLED_FEATURES"] == "security,finops,compliance,policies"


def test_the_api_started_by_the_cli_writes_jobs_to_the_jobs_table(tmp_path, monkeypatch):
    """`compass api` runs through the CLI, which installs the inline queue for its own
    commands first; the API must not take that for a host's queue (found by the e2e suite:
    every job started from the dashboard ran inside the request and failed)."""
    import json

    from compass_api import app as api_app
    from compass_core import config, dispatch, queue

    path = tmp_path / "local.json"
    path.write_text(json.dumps({"workspace_id": "local", "dynamodb_config": {"workspace_id": "local", "jobs": "compass_jobs_local"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(config, "_loader", None)
    previous = queue._factory
    try:
        dispatch.use_inline_queue()              # what cli.main does before dispatching core.api
        api_app._configure_queue()
        assert isinstance(queue._factory(), queue.StoreQueue)
        host_queue = object()
        queue.set_job_queue(lambda: host_queue)  # a host's own queue is kept
        api_app._configure_queue()
        assert queue._factory() is host_queue
    finally:
        queue.set_job_queue(previous)
