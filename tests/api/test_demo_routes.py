"""GET /api/demo: 404 outside demo mode; in demo mode the world's state (not seeded, then
seeded) beside the real scan's rows, with the links and pinned settings from the block."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compass_api.routes import demo as demo_routes
from compass_core import config as config_module
from compass_core import demo
from infra_map.graph_store import open_store
from infra_map.models import ResourceNode

ORG = "acme"
WORKSPACE = "ws-demo"


@pytest.fixture
def client(monkeypatch, tmp_path):
    config = {"workspace_id": WORKSPACE, "org_id": ORG, "aws": {"account_id": "123456789012"},
              "map": {"store": "sqlite", "path": str(tmp_path / "map.sqlite")},
              "demo": {"world": {"resources": 80, "seed": 5}, "links": {"install": "https://example.com/install"}}}
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    monkeypatch.setattr(config_module, "_loader", lambda ws: dict(config))
    monkeypatch.setattr(demo_routes, "get_workspace_config", lambda request: dict(config))
    monkeypatch.setattr(demo_routes, "resolve_infra_map_context",
                        lambda request: {"store": open_store(config, workspace_id=WORKSPACE), "default_org": ORG, "workspace_id": WORKSPACE})
    monkeypatch.setattr(demo_routes, "_cooldowns", lambda request: [])  # the trips live in the items table: tests/api/test_demo_trip.py
    app = FastAPI()
    app.include_router(demo_routes.router)
    return TestClient(app), config


def test_outside_demo_mode_the_route_is_not_there(client, monkeypatch):
    monkeypatch.setenv("APP_MODE", "production")
    http, _ = client
    assert http.get("/api/demo").status_code == 404


def test_the_demo_describes_its_world_before_and_after_the_seed(client, monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    http, config = client
    store = open_store(config, workspace_id=WORKSPACE)
    with store.begin_scan(ORG, scopes=[("aws_scan", "aws:full")]) as scan:
        scan.put_nodes([ResourceNode(org_id=ORG, resource_id="arn:aws:s3:::real", type="s3_bucket", account_id="123456789012", region="global",
                                     name="real", source="aws_scan", scope="aws:full")])
    store.close()

    before = http.get("/api/demo").json()
    assert before["mode"] == "demo" and before["world"] == {"resources": 80, "seed": 5, "scope": "demo:seed:5", "seeded": False, "rows": 0}
    assert before["map"] == [{"source": "aws_scan", "scope": "aws:full", "rows": 1, "synthetic": False}]
    assert before["links"]["install"] == "https://example.com/install" and before["links"]["repository"].startswith("https://github.com/")
    assert "github" in before["pinned_settings"] and before["cooldown_seconds"] == 300 and before["trippable"] == []

    seeded = demo.demo({"verb": "seed", "workspace_id": WORKSPACE})
    after = http.get("/api/demo").json()
    assert after["world"]["seeded"] is True and after["world"]["rows"] == seeded["nodes"] + seeded["edges"]
    assert {(r["source"], r["scope"]) for r in after["map"] if r["synthetic"]} == {("aws_scan", "demo:seed:5"), ("git_scan", "demo:seed:5"), ("tf_state", "demo:seed:5")}
    assert any(r["scope"] == "aws:full" and not r["synthetic"] for r in after["map"])
