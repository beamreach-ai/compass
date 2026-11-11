"""Who is calling: one Principal per request, resolved before the workspace (docs/specs/host-seams.md §3.1).

The open default keeps 0.9.0's behaviour: a token, else the proxy's identity header, else the
cookie session, else nobody; everyone signed in is admin. A host installs its own resolver
with ``set_principal_resolver`` and the API, ``get_actor_ids`` and ``/api/auth/me`` see it."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api.auth import principal as principals
from compass_api.auth.principal import ApiPrincipal, Principal
from compass_api.middleware.simple_auth import SimpleAuthMiddleware
from compass_api.middleware.workspace import WorkspaceContextMiddleware
from compass_api.routes import auth as auth_routes
from compass_api.routes._workspace import get_actor_ids
from compass_core import hosts


@pytest.fixture(autouse=True)
def _open_default(monkeypatch):
    principals.set_principal_resolver(None)
    monkeypatch.setattr("compass_api.workspace._user_workspace", lambda _user_id: False)
    for name in ("APP_USERNAME", "APP_PASSWORD", "DEFAULT_WORKSPACE", "TARGET"):
        monkeypatch.delenv(name, raising=False)
    yield
    principals.set_principal_resolver(None)


class _FakeBearer(BaseHTTPMiddleware):
    """An API token, without the token store: the header names the scope."""

    async def dispatch(self, request, call_next):
        scope = request.headers.get("X-Test-Token")
        if scope:
            user = None if request.headers.get("X-Test-Token-User") == "none" else "u1"
            request.state.api_principal = ApiPrincipal(token_id="tok_1", kind="personal", scope=scope, workspace_id="ws-token", user_id=user, name="t")
        return await call_next(request)


def _stack(monkeypatch, *, auth_enabled=False) -> TestClient:
    if auth_enabled:
        monkeypatch.setenv("APP_USERNAME", "admin")
        monkeypatch.setenv("APP_PASSWORD", "secret-pass")
    monkeypatch.setattr("compass_api.workspace._loader",
                        lambda workspace_id, *, org_id=None, user_id=None, **_: SimpleNamespace(workspace_id=workspace_id, org_id=org_id, user_id=user_id, config={}))
    monkeypatch.setenv("DEFAULT_WORKSPACE", "default-ws")
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.add_middleware(SimpleAuthMiddleware)
    app.add_middleware(WorkspaceContextMiddleware, optional=False)
    app.add_middleware(_FakeBearer)

    @app.get("/api/probe")
    def probe(request: Request):
        return {"actor": get_actor_ids(request), "principal": request.state.principal.to_public()}

    return TestClient(app)


def _cookie(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"}).status_code == 200


# ----------------------------------------------------------------------------- the open default


def test_nobody_is_anonymous_and_user_id_stays_none(monkeypatch):
    body = _stack(monkeypatch).get("/api/probe").json()
    assert body["actor"] == {"workspace_id": "default-ws", "org_id": None, "user_id": None, "principal_id": "anonymous",
                             "principal": body["principal"]}
    assert body["principal"] == {"id": "anonymous", "kind": "user", "workspace_id": "default-ws", "org_id": None, "email": None,
                                 "name": None, "roles": ["admin"], "teams": [], "source": "anonymous"}


def test_the_actor_spread_into_a_payload_keeps_the_payloads_keys(monkeypatch):
    """Captains spread get_actor_ids into job payloads ({**payload, **actor}): the new keys
    must not shadow a payload's own name or email (the example captain greets name=)."""
    actor = _stack(monkeypatch).get("/api/probe", headers={"X-User-Id": "sub-1"}).json()["actor"]
    assert set(actor) == {"workspace_id", "org_id", "user_id", "principal_id", "principal"}


def test_the_proxy_header_is_the_user_as_before(monkeypatch):
    body = _stack(monkeypatch).get("/api/probe", headers={"X-User-Id": "sub-1", "X-Workspace-Id": "ws-2", "X-Org-Id": "org-9"}).json()
    assert body["actor"]["user_id"] == "sub-1" and body["actor"]["workspace_id"] == "ws-2" and body["actor"]["org_id"] == "org-9"
    assert body["principal"]["source"] == "header" and body["principal"]["roles"] == ["admin"]
    assert body["actor"]["principal_id"] == "sub-1"


def test_the_cookie_session_is_app_username(monkeypatch):
    client = _stack(monkeypatch, auth_enabled=True)
    assert client.get("/api/probe").status_code == 401
    _cookie(client)
    body = client.get("/api/probe").json()
    assert body["actor"]["user_id"] == "admin" and body["principal"]["source"] == "session"
    # behind a proxy, the identity header still names the user (docs/demo.md)
    assert client.get("/api/probe", headers={"X-User-Id": "sub-1"}).json()["actor"]["user_id"] == "sub-1"


def test_a_token_is_a_principal_with_its_scope_as_the_role(monkeypatch):
    body = _stack(monkeypatch).get("/api/probe", headers={"X-Test-Token": "operate"}).json()
    assert body["actor"]["user_id"] == "u1" and body["actor"]["workspace_id"] == "ws-token"
    assert body["principal"] == {"id": "u1", "kind": "token", "workspace_id": "ws-token", "org_id": None, "email": None,
                                 "name": "t", "roles": ["operate"], "teams": [], "source": "api_token"}
    # a token minted for nobody: the principal is the token, the user id stays None as before
    body = _stack(monkeypatch).get("/api/probe", headers={"X-Test-Token": "operate", "X-Test-Token-User": "none"}).json()
    assert body["actor"]["user_id"] is None and body["actor"]["principal_id"] == "tok_1"


def test_a_users_own_workspace_comes_from_the_identity(monkeypatch):
    """The host's rule (compass_api.workspace.install(user_workspace=...)) is applied by the
    resolver, before the workspace is settled: the identity names the workspace."""
    monkeypatch.setattr("compass_api.workspace._user_workspace", lambda user_id: bool(user_id))
    body = _stack(monkeypatch).get("/api/probe", headers={"X-User-Id": "user-123", "X-Workspace-Id": "other", "X-Org-Id": "ignored"}).json()
    assert body["actor"]["workspace_id"] == "user-123" and body["actor"]["org_id"] is None
    assert body["principal"]["workspace_id"] == "user-123"


def test_me_keeps_its_keys_and_adds_the_principal(monkeypatch):
    client = _stack(monkeypatch)
    assert client.get("/api/auth/me").json() == {
        "authenticated": True, "auth_enabled": False,
        "principal": {"id": "anonymous", "kind": "user", "workspace_id": "default-ws", "org_id": None, "email": None, "name": None,
                      "roles": ["admin"], "teams": [], "source": "anonymous"},
    }
    token = client.get("/api/auth/me", headers={"X-Test-Token": "read"}).json()
    assert token["method"] == "api_token" and token["token_id"] == "tok_1" and token["scope"] == "read"
    assert token["principal"]["roles"] == ["read"] and token["principal"]["id"] == "u1"

    client = _stack(monkeypatch, auth_enabled=True)
    assert client.get("/api/auth/me").status_code == 401
    _cookie(client)
    me = client.get("/api/auth/me").json()
    assert me["username"] == "admin" and me["principal"]["id"] == "admin" and me["principal"]["source"] == "session"


# ----------------------------------------------------------------------------- a host's resolver


def _module(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def test_a_host_resolves_who_is_calling(tmp_path, monkeypatch):
    """A fake host installed through COMPASS_HOSTS: its resolver reads a header of its own and
    answers with roles and teams; /api/auth/me, get_actor_ids and the workspace show it."""
    router = APIRouter()

    @router.get("/api/hosted/actor")
    def actor(request: Request) -> dict:
        return get_actor_ids(request)

    def resolve(request, user_id, requested_workspace_id):
        who = request.headers.get("X-Host-User")
        if not who:
            return None   # the open default answers
        return Principal(id=who, kind="user", workspace_id=f"home-of-{who}", email=f"{who}@example.com", name=who.title(),
                         roles=("read",), teams=("sec",), source="host")

    def install_host():
        from compass_api import extensions
        from compass_api import workspace as api_workspace

        principals.set_principal_resolver(resolve)
        extensions.register_router(router)
        api_workspace.install(loader=lambda workspace_id, *, org_id=None, user_id=None, **_: SimpleNamespace(
            workspace_id=workspace_id, org_id=org_id, user_id=user_id, config={"workspace_id": workspace_id}))

    hosts.reset_for_tests()
    _module("fake_host", install=install_host)
    monkeypatch.setenv("COMPASS_HOSTS", "fake_host:install")
    cfg = {"workspace_id": "local", "dynamodb_config": {"workspace_id": "local", "items": "t", "jobs": "jobs_t"}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setenv("DEFAULT_WORKSPACE", "local")
    from compass_api import workspace as api_workspace
    from compass_core import config as core_config

    open_loader = api_workspace._loader
    monkeypatch.setattr(core_config, "_loader", None)
    try:
        from compass_api.app import create_app

        with TestClient(create_app()) as client:
            me = client.get("/api/auth/me", headers={"X-Host-User": "dana"}).json()["principal"]
            assert me == {"id": "dana", "kind": "user", "workspace_id": "home-of-dana", "org_id": None, "email": "dana@example.com",
                          "name": "Dana", "roles": ["read"], "teams": ["sec"], "source": "host"}
            actor = client.get("/api/hosted/actor", headers={"X-Host-User": "dana"}).json()
            assert actor == {"workspace_id": "home-of-dana", "org_id": None, "user_id": "dana", "principal_id": "dana", "principal": me}
            # without the host's header, the open default still answers
            assert client.get("/api/hosted/actor").json()["principal"]["source"] == "anonymous"
    finally:
        hosts.reset_for_tests()
        sys.modules.pop("fake_host", None)
        api_workspace.install(loader=open_loader)
