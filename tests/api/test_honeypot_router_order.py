"""The honeypots router ends with a legacy /api/honeypots/{id} catch-all, so
every router with fixed /api/honeypots/<segment> paths must be included
before it. This builds the app in the production order and checks."""

from __future__ import annotations

import re
from pathlib import Path

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api.routes import honeypot_actions, honeypot_incidents, honeypots
from compass_core.keys import workspace_pk

APP = Path(__file__).resolve().parents[2] / "compass_api" / "app.py"


def _include_order():
    text = APP.read_text()
    return [m.group(1) for m in re.finditer(r"^\s+(\w+)\.router,$", text, re.MULTILINE)]


def test_fixed_segment_routers_are_included_before_the_catch_all():
    order = _include_order()
    assert order.index("honeypot_actions") < order.index("honeypots")
    assert order.index("honeypot_incidents") < order.index("honeypots")


@pytest.fixture
def client(monkeypatch):
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
        pk = workspace_pk("ws-a")
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: pk)
        monkeypatch.setattr(honeypots, "get_actor_ids", lambda request: {"workspace_id": "ws-a"})
        monkeypatch.setattr(honeypots, "get_workspace_config", lambda request: {})
        monkeypatch.setattr(honeypot_actions, "get_actor_ids", lambda request: {"workspace_id": "ws-a", "user_id": "u"})
        app = FastAPI()
        for name in _include_order():
            mod = {"honeypot_actions": honeypot_actions, "honeypot_incidents": honeypot_incidents, "honeypots": honeypots}.get(name)
            if mod is not None:
                app.include_router(mod.router)
        yield TestClient(app, follow_redirects=False)


def test_fixed_paths_reach_their_routers_in_production_order(client):
    assert client.get("/api/honeypots/actions").status_code == 200
    assert client.get("/api/honeypots/audits").status_code == 200
    assert client.get("/api/honeypots/settings").status_code == 200
    assert client.get("/api/honeypots/incidents").status_code == 200
    assert client.get("/api/honeypots/actions/catalog").status_code == 200
    # the legacy redirect still works for an id-shaped path
    assert client.get("/api/honeypots/abc-123").status_code == 308
