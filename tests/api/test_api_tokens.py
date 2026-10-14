"""API tokens: store, scopes, bearer middleware and the token routes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from compass_api import extensions
from compass_api.auth import tokens as token_store
from compass_api.auth.scopes import required_scope
from compass_api.middleware.bearer_auth import BearerAuthMiddleware
from compass_api.middleware.simple_auth import SimpleAuthMiddleware
from compass_api.middleware.workspace import WorkspaceContextMiddleware


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    monkeypatch.delenv("WORKSPACES_TABLE", raising=False)
    monkeypatch.delenv("BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("BOOTSTRAP_TOKEN_SECRET_ARN", raising=False)
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    token_store.reset_store_for_tests()
    yield
    token_store.reset_store_for_tests()


# ----------------------------------------------------------------------------- store


def test_create_list_resolve_revoke_roundtrip():
    created = token_store.create_token(workspace_id="ws-a", name="laptop", scope="operate", user_id="ilia")
    assert created["token"].startswith("cmp_") and len(created["token"]) > 40
    assert created["scope"] == "operate"

    principal = token_store.resolve_token(created["token"])
    assert principal is not None
    assert principal.workspace_id == "ws-a" and principal.user_id == "ilia" and principal.kind == "personal"
    assert principal.has_scope("read") and principal.has_scope("operate") and not principal.has_scope("admin")

    listed = token_store.list_tokens("ws-a")
    assert [t["token_id"] for t in listed] == [created["token_id"]]
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    assert token_store.revoke_token("ws-a", created["token_id"])["revoked_at"]
    assert token_store.resolve_token(created["token"]) is None
    assert token_store.revoke_token("ws-a", "tok_missing") is None


def test_unknown_and_malformed_tokens_do_not_resolve():
    assert token_store.resolve_token(None) is None
    assert token_store.resolve_token("not-a-token") is None
    assert token_store.resolve_token("cmp_" + "a" * 43) is None


def test_bootstrap_token_from_env_is_admin_for_default_workspace(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "cmp_bootstrap-secret-value")
    monkeypatch.setenv("DEFAULT_WORKSPACE", "beamreach")
    principal = token_store.resolve_token("cmp_bootstrap-secret-value")
    assert principal is not None
    assert principal.kind == "bootstrap" and principal.scope == "admin" and principal.workspace_id == "beamreach"
    assert token_store.resolve_token("cmp_bootstrap-secret-valuX") is None


def test_invalid_scope_is_rejected():
    with pytest.raises(ValueError):
        token_store.create_token(workspace_id="ws", name="x", scope="root")


# ----------------------------------------------------------------------------- scope policy


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/prs", "read"),
        ("GET", "/api/status", "read"),
        ("GET", "/api/honeypots/tokens/abc/material", "operate"),
        ("GET", "/api/auth/tokens", "admin"),
        ("POST", "/api/honeypots/tokens", "operate"),
        ("POST", "/api/prs/acme/infra/7/address", "operate"),
        ("POST", "/api/honeypots/tokens/abc/execute-runbook", "operate"),
        ("POST", "/api/settings/aws", "admin"),
        ("PUT", "/api/prs/watcher", "admin"),
        ("PUT", "/api/honeypots/settings", "admin"),
        ("POST", "/api/auth/tokens", "admin"),
        ("DELETE", "/api/auth/tokens/tok_1", "admin"),
        ("GET", "/health", None),
        ("GET", "/api/auth/me", None),
    ],
)
def test_required_scope(method, path, expected):
    assert required_scope(method, path) == expected


# ----------------------------------------------------------------------------- middleware stack


def _stack(monkeypatch, *, auth_enabled: bool = False) -> TestClient:
    if auth_enabled:
        monkeypatch.setenv("APP_USERNAME", "admin")
        monkeypatch.setenv("APP_PASSWORD", "secret-pass")

    def fake_load(workspace_id, *, org_id=None, user_id=None, force_refresh=False):
        return SimpleNamespace(workspace_id=workspace_id, org_id=org_id, user_id=user_id, config={})

    monkeypatch.setattr("compass_api.workspace._loader", fake_load)
    monkeypatch.setenv("DEFAULT_WORKSPACE", "default-ws")

    app = FastAPI()
    app.add_middleware(SimpleAuthMiddleware)
    app.add_middleware(WorkspaceContextMiddleware, optional=False)
    app.add_middleware(BearerAuthMiddleware)

    @app.get("/api/probe")
    def probe(request: Request):
        ctx = request.state.workspace_ctx
        principal = getattr(request.state, "api_principal", None)
        return {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "token_id": principal.token_id if principal else None}

    @app.post("/api/probe")
    def probe_write(request: Request):
        return {"ok": True}

    @app.post("/api/settings/aws")
    def admin_write(request: Request):
        return {"ok": True}

    @app.post("/triage/api/triage")
    def triage(request: Request):
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def test_token_sets_workspace_and_user(monkeypatch):
    client = _stack(monkeypatch)
    created = token_store.create_token(workspace_id="ws-token", name="t", scope="read", user_id="u1")
    response = client.get("/api/probe", headers={"Authorization": f"Bearer {created['token']}"})
    assert response.status_code == 200
    assert response.json() == {"workspace_id": "ws-token", "user_id": "u1", "token_id": created["token_id"]}


def test_token_rejects_conflicting_workspace_header(monkeypatch):
    client = _stack(monkeypatch)
    created = token_store.create_token(workspace_id="ws-token", name="t", scope="read")
    response = client.get(
        "/api/probe",
        headers={"Authorization": f"Bearer {created['token']}", "X-Workspace-Id": "other"},
    )
    assert response.status_code == 403
    # the same workspace in the header is fine
    response = client.get(
        "/api/probe",
        headers={"Authorization": f"Bearer {created['token']}", "X-Workspace-Id": "ws-token"},
    )
    assert response.status_code == 200


def test_scope_enforced_for_tokens(monkeypatch):
    client = _stack(monkeypatch)
    read = token_store.create_token(workspace_id="ws", name="r", scope="read")
    operate = token_store.create_token(workspace_id="ws", name="o", scope="operate")
    admin = token_store.create_token(workspace_id="ws", name="a", scope="admin")

    assert client.post("/api/probe", headers={"Authorization": f"Bearer {read['token']}"}).status_code == 403
    assert client.post("/api/probe", headers={"Authorization": f"Bearer {operate['token']}"}).status_code == 200
    assert client.post("/api/settings/aws", headers={"Authorization": f"Bearer {operate['token']}"}).status_code == 403
    body = client.post("/api/settings/aws", headers={"Authorization": f"Bearer {operate['token']}"}).json()
    assert body["required_scope"] == "admin" and body["token_scope"] == "operate"
    assert client.post("/api/settings/aws", headers={"Authorization": f"Bearer {admin['token']}"}).status_code == 200


def test_invalid_token_is_401_even_when_auth_disabled(monkeypatch):
    client = _stack(monkeypatch)
    response = client.get("/api/probe", headers={"Authorization": "Bearer cmp_definitely-not-real"})
    assert response.status_code == 401
    # a non-compass bearer passes through to whatever auth is configured (none here)
    assert client.get("/api/probe", headers={"Authorization": "Bearer something-else"}).status_code == 200


def test_session_auth_gates_triage_and_accepts_tokens(monkeypatch):
    client = _stack(monkeypatch, auth_enabled=True)
    assert client.get("/health").status_code == 200
    assert client.get("/api/probe").status_code == 401
    assert client.post("/triage/api/triage").status_code == 200, "only /api is gated until a deployment registers more"
    extensions.register_gated_prefixes("/triage/")
    try:
        assert client.post("/triage/api/triage").status_code == 401, "a registered prefix is gated like /api"
    finally:
        extensions.reset_for_tests()

    created = token_store.create_token(workspace_id="ws", name="t", scope="operate")
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert client.get("/api/probe", headers=headers).status_code == 200
    assert client.post("/triage/api/triage", headers=headers).status_code == 200


# ----------------------------------------------------------------------------- routes on the real app


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    from compass_core import config as core_config
    from compass_core import queue

    def fake_load(workspace_id, *, org_id=None, user_id=None, force_refresh=False):
        return SimpleNamespace(workspace_id=workspace_id, org_id=org_id, user_id=user_id, config={}, metadata=None)

    # The open app reads one workspace config file; no items table, so the token store stays in memory.
    path = tmp_path / "beamreach.json"
    path.write_text(json.dumps({"workspace_id": "beamreach"}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    for name in ("DISABLED_FEATURES", "TARGET"):
        # set-then-delete records the original state, so what create_app() sets is undone afterwards
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr("compass_api.workspace._loader", fake_load)
    monkeypatch.setenv("DEFAULT_WORKSPACE", "beamreach")
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "cmp_bootstrap-for-tests")
    previous = queue._factory
    try:
        from compass_api.app import create_app

        yield TestClient(create_app())
    finally:
        queue.set_job_queue(previous)


def test_token_routes_bootstrap_mints_personal_token(app_client):
    bootstrap = {"Authorization": "Bearer cmp_bootstrap-for-tests"}

    me = app_client.get("/api/auth/me", headers=bootstrap)
    assert me.status_code == 200
    assert me.json()["method"] == "api_token" and me.json()["kind"] == "bootstrap" and me.json()["workspace_id"] == "beamreach"

    created = app_client.post("/api/auth/tokens", json={"name": "ops@laptop", "scope": "operate"}, headers=bootstrap)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["token"].startswith("cmp_") and body["workspace_id"] == "beamreach" and body["user_id"] == "bootstrap"

    personal = {"Authorization": f"Bearer {body['token']}"}
    assert app_client.get("/api/auth/me", headers=personal).json()["token_id"] == body["token_id"]
    # operate scope cannot list or mint tokens
    assert app_client.get("/api/auth/tokens", headers=personal).status_code == 403
    assert app_client.post("/api/auth/tokens", json={"name": "x"}, headers=personal).status_code == 403

    listed = app_client.get("/api/auth/tokens", headers=bootstrap)
    assert listed.status_code == 200
    assert [t["token_id"] for t in listed.json()["items"]] == [body["token_id"]]
    assert listed.json()["bootstrap_configured"] is True

    revoked = app_client.delete(f"/api/auth/tokens/{body['token_id']}", headers=bootstrap)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert app_client.get("/api/auth/me", headers=personal).status_code == 401
    # revoking again is idempotent
    again = app_client.delete(f"/api/auth/tokens/{body['token_id']}", headers=bootstrap)
    assert again.status_code == 200 and again.json()["revoked_at"]
    assert app_client.delete("/api/auth/tokens/tok_missing", headers=bootstrap).status_code == 404


def test_token_cannot_revoke_itself(app_client):
    bootstrap = {"Authorization": "Bearer cmp_bootstrap-for-tests"}
    created = app_client.post("/api/auth/tokens", json={"name": "a", "scope": "admin"}, headers=bootstrap).json()
    own = {"Authorization": f"Bearer {created['token']}"}
    assert app_client.delete(f"/api/auth/tokens/{created['token_id']}", headers=own).status_code == 400


def test_dashboard_session_can_manage_tokens_without_principal(app_client):
    # No APP_USERNAME/APP_PASSWORD: the open (local) mode behaves like a dashboard session.
    created = app_client.post("/api/auth/tokens", json={"name": "from-ui", "scope": "read"}, headers={"X-Workspace-Id": "beamreach"})
    assert created.status_code == 201
    assert created.json()["user_id"] == "dashboard"
