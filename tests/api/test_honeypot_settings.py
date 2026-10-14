"""Honeypot settings (the Autopilot flag) and the read-only write guard."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compass_api.routes import honeypot_actions, honeypot_incidents, honeypots, notifications


@pytest.fixture
def client(monkeypatch):
    config: dict = {"aws": {"account_id": "123456789012", "region": "us-east-1"}}
    saved = []
    monkeypatch.setattr(honeypots, "get_workspace_config", lambda request: config)
    monkeypatch.setattr(honeypots, "save_config", lambda request, cfg: saved.append(dict(cfg)))
    app = FastAPI()
    app.include_router(honeypot_incidents.router)
    app.include_router(honeypot_actions.router)
    app.include_router(notifications.router)
    app.include_router(honeypots.router)
    return TestClient(app, follow_redirects=False), config, saved


def test_autopilot_defaults_off_and_persists(client):
    c, config, saved = client
    assert c.get("/api/honeypots/settings").json() == {"autopilot": False}
    assert c.put("/api/honeypots/settings", json={"autopilot": True}).json() == {"autopilot": True}
    assert config["honeypot"]["autopilot"] is True and len(saved) == 1
    assert c.get("/api/honeypots/settings").json() == {"autopilot": True}
    assert c.put("/api/honeypots/settings", json={"autopilot": False}).json() == {"autopilot": False}


def test_settings_is_a_fixed_segment_not_a_legacy_id(client):
    c, _, _ = client
    assert c.post("/api/honeypots/settings").status_code == 405, "no redirect to /tokens/settings"


def test_read_only_refuses_writes_but_not_reads_or_inbound_alerts(client, monkeypatch):
    c, _, saved = client
    monkeypatch.setenv("READ_ONLY", "true")
    assert c.get("/api/honeypots/settings").status_code == 200
    assert c.put("/api/honeypots/settings", json={"autopilot": True}).status_code == 403
    assert c.post("/api/honeypots/tokens", json={"name": "x"}).status_code == 403
    assert c.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task"}).status_code == 403
    assert c.post("/api/honeypots/incidents/1-abc/close").status_code == 403
    assert c.post("/api/notifications/endpoints", json={}).status_code == 403
    assert saved == []
    # The pipeline's own write stays open: it fails on validation, not on the guard.
    assert c.post("/api/honeypots/inbound-alert", json={}).status_code != 403
