"""Slack as a workspace integration: OAuth v2 connect, channels, disconnect, alert channels."""

from __future__ import annotations

import json
import urllib.parse

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.honeypot import alert_dispatcher as ad
from compass_api.routes import notifications as n
from compass_api.routes import slack_connect as sc
from compass_core.keys import notification_endpoint_prefix, workspace_pk

PK = workspace_pk("ws-a")
TOKEN = "xoxb-secret-token"
OAUTH_OK = {
    "ok": True,
    "access_token": TOKEN,
    "token_type": "bot",
    "scope": sc.SLACK_SCOPES,
    "bot_user_id": "U0BOT",
    "team": {"id": "T1", "name": "Acme"},
}
HEADERS = {"x-forwarded-proto": "https", "host": "demo.example.org"}


class FakeSlack:
    """Stands in for slack_connect._http_form; routes by URL."""

    def __init__(self):
        self.calls: list = []
        self.oauth = dict(OAUTH_OK)
        self.pages: list = []
        self.revoke = {"ok": True, "revoked": True}

    def __call__(self, url, form, *, token=None, timeout=10.0):
        self.calls.append((url, dict(form), token))
        if url == sc.SLACK_OAUTH_ACCESS_URL:
            return self.oauth
        if url == sc.SLACK_CONVERSATIONS_LIST_URL:
            return self.pages.pop(0) if self.pages else {"ok": True, "channels": []}
        if url == sc.SLACK_AUTH_REVOKE_URL:
            if isinstance(self.revoke, Exception):
                raise self.revoke
            return self.revoke
        raise AssertionError(url)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("READ_ONLY", raising=False)
    monkeypatch.delenv("SLACK_REDIRECT_URI", raising=False)
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "shh")
    sc.reset_slack_app_cache()
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        config: dict = {"billing": {"install_id": "i-1"}}
        saved: list = []
        monkeypatch.setattr(n, "_resolve_table", lambda request: table)
        monkeypatch.setattr(n, "_ws_pk", lambda request: PK)
        monkeypatch.setattr(n, "get_workspace_config", lambda request: config)
        monkeypatch.setattr(sc, "get_workspace_config", lambda request: config)
        monkeypatch.setattr(sc, "save_config", lambda request, cfg: saved.append(json.loads(json.dumps(cfg))))
        monkeypatch.setattr(sc, "get_actor_ids", lambda request: {"workspace_id": "ws-a", "user_id": "u-1", "email": "owner@example.org"})
        slack = FakeSlack()
        monkeypatch.setattr(sc, "_http_form", slack)
        posts: list = []
        monkeypatch.setattr(
            ad, "_http_json",
            lambda url, payload, headers=None, timeout=10.0: posts.append((url, payload, headers)) or {"ok": True, "channel": "C123", "ts": "1.0"},
        )
        app = FastAPI()
        app.include_router(n.router)
        app.include_router(sc.router)
        yield TestClient(app, follow_redirects=False), table, config, saved, slack, posts
    sc.reset_slack_app_cache()


def _endpoints(table):
    return table.query(KeyConditionExpression=Key("pk").eq(PK) & Key("sk").begins_with(notification_endpoint_prefix()))["Items"]


def _install_state(client) -> tuple[str, dict]:
    r = client.get("/api/integrations/slack/install", headers=HEADERS)
    assert r.status_code == 302
    url = urllib.parse.urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == sc.SLACK_AUTHORIZE_URL
    q = dict(urllib.parse.parse_qsl(url.query))
    return q["state"], q


def _callback(client, state, code="c-1", **extra):
    params = {"code": code, "state": state, **extra}
    return client.get("/api/integrations/slack/callback", params=params, headers=HEADERS)


def _connect(client):
    state, _ = _install_state(client)
    r = _callback(client, state)
    assert r.headers["location"] == "/workspace?tab=settings&slack=connected"
    return r


# ── status / app credentials ───────────────────────────────────────────────

def test_status_available_and_connected(env, monkeypatch):
    client, _, config, *_ = env
    assert client.get("/api/integrations/slack/status").json() == {"available": True, "connected": False}
    config["slack"] = {"team_id": "T1", "team_name": "Acme", "bot_token": TOKEN, "connected_at": "2026-10-07T10:00:00+00:00"}
    body = client.get("/api/integrations/slack/status").json()
    assert body == {"available": True, "connected": True, "team_id": "T1", "team_name": "Acme", "connected_at": "2026-10-07T10:00:00+00:00"}
    assert TOKEN not in json.dumps(body)

    monkeypatch.delenv("SLACK_CLIENT_ID")
    monkeypatch.delenv("SLACK_CLIENT_SECRET")
    monkeypatch.setattr(sc, "_read_slack_app", lambda: None)
    sc.reset_slack_app_cache()
    assert client.get("/api/integrations/slack/status").json()["available"] is False
    r = client.get("/api/integrations/slack/install")
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_app_read_from_secrets_manager(env, monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID")
    monkeypatch.delenv("SLACK_CLIENT_SECRET")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    sc.reset_slack_app_cache()
    assert sc.load_slack_app() is None  # no secret yet: quietly unavailable
    boto3.client("secretsmanager", region_name="eu-central-1").create_secret(
        Name="beamreach/slack-app", SecretString='{"client_id": "9.9", "client_secret": "s", "signing_secret": "x"}'
    )
    sc.reset_slack_app_cache()
    assert sc.load_slack_app() == {"client_id": "9.9", "client_secret": "s"}


# ── install / callback ─────────────────────────────────────────────────────

def test_install_redirect_and_callback_stores_connection(env):
    client, table, config, saved, slack, posts = env
    state, q = _install_state(client)
    assert q["client_id"] == "111.222"
    assert q["scope"] == "chat:write,chat:write.public,channels:read,groups:read"
    assert "incoming-webhook" not in q["scope"]
    assert q["redirect_uri"] == "https://demo.example.org/api/integrations/slack/callback"

    r = _callback(client, state)
    assert r.status_code == 302
    assert r.headers["location"] == "/workspace?tab=settings&slack=connected"
    assert TOKEN not in r.text and TOKEN not in r.headers["location"]
    url, form, _ = slack.calls[0]
    assert url == sc.SLACK_OAUTH_ACCESS_URL
    assert form == {"client_id": "111.222", "client_secret": "shh", "code": "c-1", "redirect_uri": q["redirect_uri"]}

    stored = saved[-1]["slack"]
    assert stored["bot_token"] == TOKEN and stored["team_id"] == "T1" and stored["team_name"] == "Acme"
    assert stored["bot_user_id"] == "U0BOT" and stored["scopes"] == sc.SLACK_SCOPES
    assert stored["connected_by"] == "owner@example.org" and stored["connected_at"]
    assert saved[-1]["billing"] == {"install_id": "i-1"}  # rest of the config kept
    # no endpoint is created and no message is posted on connect
    assert _endpoints(table) == [] and posts == []

    status = client.get("/api/integrations/slack/status")
    assert status.json()["connected"] is True and TOKEN not in status.text


def test_reconnect_replaces_the_token(env):
    client, _, config, saved, slack, _ = env
    _connect(client)
    slack.oauth = {**OAUTH_OK, "access_token": "xoxb-rotated"}
    _connect(client)
    assert config["slack"]["bot_token"] == "xoxb-rotated" and len(saved) == 2


def test_tampered_or_expired_state_is_refused(env):
    client, _, config, saved, slack, _ = env
    state, _ = _install_state(client)
    body, sig = state.split(".")
    r = _callback(client, f"{body}x.{sig}")
    assert r.status_code == 302 and r.headers["location"] == "/workspace?tab=settings&slack=error&reason=invalid_state"

    expired = sc.make_state("ws-a", "u-1", "shh", now=0)
    assert _callback(client, expired).headers["location"].endswith("reason=expired_state")

    other = sc.make_state("ws-b", "u-1", "shh")
    assert _callback(client, other).headers["location"].endswith("reason=workspace_mismatch")

    wrong_key = sc.make_state("ws-a", "u-1", "not-the-secret")
    assert _callback(client, wrong_key).headers["location"].endswith("reason=invalid_state")

    assert slack.calls == [] and saved == [] and "slack" not in config


def test_slack_not_ok_and_user_denial(env):
    client, _, config, saved, slack, _ = env
    slack.oauth = {"ok": False, "error": "invalid_code"}
    state, _ = _install_state(client)
    assert _callback(client, state).headers["location"] == "/workspace?tab=settings&slack=error&reason=invalid_code"

    r = client.get("/api/integrations/slack/callback", params={"error": "access_denied", "state": state})
    assert r.headers["location"].endswith("slack=error&reason=access_denied")

    slack.oauth = {**OAUTH_OK, "access_token": "xoxp-user-token"}
    assert _callback(client, state).headers["location"].endswith("reason=no_bot_token")
    assert saved == [] and "slack" not in config


def test_read_only_refuses_install_callback_and_writes(env, monkeypatch):
    client, table, config, saved, *_ = env
    state, _ = _install_state(client)
    config["slack"] = {"team_id": "T1", "bot_token": TOKEN}
    monkeypatch.setenv("READ_ONLY", "true")
    assert client.get("/api/integrations/slack/install").status_code == 403
    assert _callback(client, state).headers["location"].endswith("reason=read_only")
    assert client.delete("/api/integrations/slack").status_code == 403
    assert client.post("/api/integrations/slack/endpoints", json={"channel": "C1"}).status_code == 403
    assert client.get("/api/integrations/slack/status").status_code == 200
    assert saved == [] and config["slack"]["bot_token"] == TOKEN and _endpoints(table) == []


def test_redirect_uri_from_env(env, monkeypatch):
    client, *_ = env
    monkeypatch.setenv("SLACK_REDIRECT_URI", "https://compass.example.com/api/integrations/slack/callback")
    _, q = _install_state(client)
    assert q["redirect_uri"] == "https://compass.example.com/api/integrations/slack/callback"


# ── channels ───────────────────────────────────────────────────────────────

def test_channels_need_a_connection(env):
    client, *_ = env
    r = client.get("/api/integrations/slack/channels")
    assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_channels_paginate_and_sort_public_first(env):
    client, _, config, _, slack, _ = env
    config["slack"] = {"team_id": "T1", "bot_token": TOKEN}
    slack.pages = [
        {"ok": True, "channels": [
            {"id": "G1", "name": "incident-room", "is_private": True, "is_member": False},
            {"id": "C2", "name": "zeta", "is_private": False, "is_member": False},
        ], "response_metadata": {"next_cursor": "cur-2"}},
        {"ok": True, "channels": [
            {"id": "C1", "name": "Alerts", "is_private": False, "is_member": True},
            {"id": "G2", "name": "blue-team", "is_private": True, "is_member": True},
        ], "response_metadata": {"next_cursor": ""}},
    ]
    r = client.get("/api/integrations/slack/channels")
    assert r.status_code == 200
    assert r.json() == {"channels": [
        {"id": "C1", "name": "Alerts", "is_private": False, "is_member": True},
        {"id": "C2", "name": "zeta", "is_private": False, "is_member": False},
        {"id": "G2", "name": "blue-team", "is_private": True, "is_member": True},
        {"id": "G1", "name": "incident-room", "is_private": True, "is_member": False},
    ]}
    (u1, f1, t1), (u2, f2, t2) = slack.calls
    assert u1 == sc.SLACK_CONVERSATIONS_LIST_URL and t1 == TOKEN
    assert f1["types"] == "public_channel,private_channel" and f1["exclude_archived"] == "true" and "cursor" not in f1
    assert f2["cursor"] == "cur-2"
    assert TOKEN not in r.text


def test_channels_are_capped(env, monkeypatch):
    client, _, config, _, slack, _ = env
    config["slack"] = {"team_id": "T1", "bot_token": TOKEN}
    monkeypatch.setattr(sc, "CHANNELS_CAP", 3)
    page = lambda i: {"ok": True, "channels": [{"id": f"C{i}{j}", "name": f"c{i}{j}"} for j in range(2)], "response_metadata": {"next_cursor": "more"}}
    slack.pages = [page(1), page(2), page(3)]
    assert len(client.get("/api/integrations/slack/channels").json()["channels"]) == 3
    assert len(slack.calls) == 2


def test_channels_revoked_token_asks_to_reconnect(env):
    client, _, config, _, slack, _ = env
    config["slack"] = {"team_id": "T1", "bot_token": TOKEN}
    slack.pages = [{"ok": False, "error": "token_revoked"}]
    r = client.get("/api/integrations/slack/channels")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]


# ── disconnect ─────────────────────────────────────────────────────────────

def test_disconnect_revokes_and_clears(env):
    client, _, config, saved, slack, _ = env
    _connect(client)
    r = client.delete("/api/integrations/slack")
    assert r.status_code == 200 and r.json() == {"connected": False, "revoked": True}
    assert slack.calls[-1] == (sc.SLACK_AUTH_REVOKE_URL, {}, TOKEN)
    assert "slack" not in config and "slack" not in saved[-1]
    assert client.get("/api/integrations/slack/status").json()["connected"] is False


def test_disconnect_clears_even_when_revoke_fails(env):
    client, _, config, _, slack, _ = env
    _connect(client)
    slack.revoke = OSError("down")
    r = client.delete("/api/integrations/slack")
    assert r.status_code == 200 and r.json()["revoked"] is False and "slack" not in config


# ── alert channels on the workspace connection ─────────────────────────────

def test_alert_channel_needs_a_connection(env):
    client, table, *_ = env
    r = client.post("/api/integrations/slack/endpoints", json={"channel": "C1", "channel_name": "alerts"})
    assert r.status_code == 409 and _endpoints(table) == []


def test_alert_channel_created_listed_tested_without_token(env):
    client, table, config, _, _, posts = env
    _connect(client)
    r = client.post("/api/integrations/slack/endpoints", json={"channel": "G7", "channel_name": "#blue-team", "is_private": True})
    assert r.status_code == 200
    ep = r.json()
    assert ep["type"] == "slack" and ep["use_workspace_slack"] is True and ep["channel"] == "G7"
    assert ep["channel_name"] == "blue-team" and ep["name"] == "Slack #blue-team" and ep["is_private"] is True
    stored = _endpoints(table)
    assert len(stored) == 1 and "bot_token" not in stored[0] and "webhook_url" not in stored[0]

    # same channel again: no duplicate
    again = client.post("/api/integrations/slack/endpoints", json={"channel": "G7", "channel_name": "blue-team"}).json()
    assert again["endpoint_id"] == ep["endpoint_id"] and len(_endpoints(table)) == 1

    listed = client.get("/api/notifications/endpoints").json()
    assert [e["endpoint_id"] for e in listed] == [ep["endpoint_id"]] and TOKEN not in json.dumps(listed)

    t = client.post(f"/api/integrations/slack/endpoints/{ep['endpoint_id']}/test")
    assert t.status_code == 200 and t.json()["status"] == "sent"
    url, payload, headers = posts[-1]
    assert url == ad.SLACK_POST_MESSAGE_URL and payload["channel"] == "G7" and headers["Authorization"] == f"Bearer {TOKEN}"

    # disconnected: the test explains instead of posting
    client.delete("/api/integrations/slack")
    t = client.post(f"/api/integrations/slack/endpoints/{ep['endpoint_id']}/test")
    assert t.status_code == 409 and "not connected" in t.json()["detail"]


def test_alert_channel_test_reports_invite_hint(env, monkeypatch):
    client, *_ = env
    _connect(client)
    ep = client.post("/api/integrations/slack/endpoints", json={"channel": "G7", "channel_name": "x", "is_private": True}).json()
    monkeypatch.setattr(ad, "_http_json", lambda *a, **k: {"ok": False, "error": "not_in_channel"})
    t = client.post(f"/api/integrations/slack/endpoints/{ep['endpoint_id']}/test")
    assert t.status_code == 502 and "invite @Beamreach Compass to the channel" in t.json()["detail"]


def test_alert_channel_test_only_for_workspace_slack(env):
    client, *_ = env
    other = client.post("/api/notifications/endpoints", json={"name": "w", "type": "webhook", "webhook_url": "https://x"}).json()
    assert client.post(f"/api/integrations/slack/endpoints/{other['endpoint_id']}/test").status_code == 404
