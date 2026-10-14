"""HP-28 / HP-13: endpoint types, default email endpoint, no license gate."""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api.routes import notifications as n
from compass_core.keys import notification_endpoint_sort_key, workspace_pk

PK = workspace_pk("ws-a")


@pytest.fixture
def env(monkeypatch):
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
        config: dict = {}
        saved = []
        monkeypatch.setattr(n, "_resolve_table", lambda request: table)
        monkeypatch.setattr(n, "_ws_pk", lambda request: PK)
        monkeypatch.setattr(n, "get_workspace_config", lambda request: config)
        import compass_api.routes._workspace as ws

        monkeypatch.setattr(ws, "save_config", lambda request, cfg: saved.append(dict(cfg)))
        monkeypatch.setattr(ws, "get_actor_ids", lambda request: {"workspace_id": "ws-a", "email": "owner@example.org"})
        app = FastAPI()
        app.include_router(n.router)
        yield TestClient(app), table, config, saved


def test_default_email_endpoint_is_created_once(env):
    client, _, config, saved = env
    first = client.get("/api/notifications/endpoints").json()
    assert len(first) == 1
    assert first[0]["type"] == "beamreach_email" and first[0]["recipients"] == ["owner@example.org"]
    assert first[0]["is_default"] is True
    assert config["billing"]["install_id"] and saved
    second = client.get("/api/notifications/endpoints").json()
    assert [e["endpoint_id"] for e in second] == [first[0]["endpoint_id"]]
    # deleting the default does not resurrect it
    client.delete(f"/api/notifications/endpoints/{first[0]['endpoint_id']}")
    assert client.get("/api/notifications/endpoints").json() == []


def test_default_skipped_when_endpoints_exist_or_no_owner(env, monkeypatch):
    client, table, config, _ = env
    client.post("/api/notifications/endpoints", json={"name": "w", "type": "webhook", "webhook_url": "https://x"})
    assert len(client.get("/api/notifications/endpoints").json()) == 1
    assert n.ensure_default_email_endpoint(table, PK, {}, None) is None


def test_create_smtp_hides_password_and_validates(env):
    client, table, *_ = env
    body = {"name": "mail", "type": "smtp", "host": "mail.local", "use_tls": True, "username": "u", "password": "secret",
            "from_address": "c@example.org", "recipients": ["a@example.org"]}
    out = client.post("/api/notifications/endpoints", json=body).json()
    assert out["port"] == 587 and "password" not in out and out["username"] == "u"
    stored = table.get_item(Key={"pk": PK, "sk": notification_endpoint_sort_key(out["endpoint_id"])}).get("Item")
    assert stored["password"] == "secret"
    for bad in ({"host": ""}, {"from_address": "nope"}, {"use_ssl": True}, {"recipients": []}, {"port": 0}, {"password": None}):
        r = client.post("/api/notifications/endpoints", json={**body, **bad})
        assert r.status_code == 400, bad


def test_slack_bot_token_never_returned(env):
    client, *_ = env
    assert client.post("/api/notifications/endpoints", json={"name": "s", "type": "slack", "bot_token": "xoxb-1"}).status_code == 400
    out = client.post("/api/notifications/endpoints", json={"name": "s", "type": "slack", "bot_token": "xoxb-1", "channel": "#sec"}).json()
    assert "bot_token" not in out and out["bot_mode"] is True and out["channel"] == "sec"
    assert client.post("/api/notifications/endpoints", json={"name": "s", "type": "slack"}).status_code == 400


def test_smtp_test_action(env, monkeypatch):
    client, *_ = env
    ep = client.post("/api/notifications/endpoints", json={"name": "m", "type": "smtp", "host": "h", "from_address": "c@e.org",
                                                           "recipients": ["a@e.org"]}).json()
    import captains.honeypot.alert_dispatcher as ad

    sent = []
    monkeypatch.setattr(ad, "_send_smtp", lambda item, **kw: sent.append((item["host"], kw["subject"])))
    assert client.post(f"/api/notifications/endpoints/{ep['endpoint_id']}/test").json()["status"] == "sent"
    assert sent and sent[0][0] == "h"
