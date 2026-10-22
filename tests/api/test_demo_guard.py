"""Demo mode: the operator's settings are the operator's (compass_api.demo_guard)."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from compass_api import demo_guard, extensions
from compass_api.auth.principal import ApiPrincipal

ADMIN = "ilia@beamreach.cloud"
CONFIG = {"workspace_id": "ws", "demo": {"admins": ["Ilia@Beamreach.cloud"]}}


def _oidc(email: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class _Context(BaseHTTPMiddleware):
    """What the workspace and bearer middleware leave on the request."""

    async def dispatch(self, request, call_next):
        request.state.workspace_ctx = type("Ctx", (), {"config": CONFIG, "workspace_id": "ws"})()
        scope = request.headers.get("x-test-token-scope")
        if scope:
            request.state.api_principal = ApiPrincipal(token_id="t", kind="personal", scope=scope, workspace_id="ws")
        return await call_next(request)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    extensions.reset_for_tests()
    extensions.register_identity_headers("x-amzn-oidc-identity")
    extensions.register_demo_admin_prefixes("/api/remediate-all")
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def anything(path: str):
        return {"ok": path}

    app.add_middleware(demo_guard.DemoAdminMiddleware)
    app.add_middleware(_Context)
    yield TestClient(app)
    extensions.reset_for_tests()


VISITOR_REFUSED = [
    ("POST", "/api/settings/aws"),
    ("POST", "/api/settings/limits"),
    ("PUT", "/api/honeypots/settings"),
    ("POST", "/api/honeypots/actions"),
    ("POST", "/api/honeypots/autopilot"),
    ("POST", "/api/access/grants/g1/accept"),
    ("POST", "/api/auth/tokens"),
    ("POST", "/api/captains/install"),
    ("POST", "/api/notifications/endpoints"),
    ("POST", "/api/integrations/slack/endpoints"),
    ("POST", "/api/maps/reindex"),
    ("POST", "/api/iac/fix-drift"),
    ("POST", "/api/prs/sync"),
    ("POST", "/api/honeypots/tokens/t1/arm"),
    ("PUT", "/api/honeypots/tokens/t1"),
    ("DELETE", "/api/honeypots/tokens/t1"),
    ("POST", "/api/honeypots/incidents/i1/close"),
    ("POST", "/api/honeypots/incidents/i1/respond"),
    ("GET", "/api/honeypots/tokens/t1/material"),
    ("GET", "/api/auth/tokens"),
    ("POST", "/api/remediate-all"),  # a larger deployment's own
]
VISITOR_ALLOWED = [
    ("GET", "/api/maps"),
    ("GET", "/api/demo"),
    ("GET", "/api/settings/bootstrap"),
    ("POST", "/api/iac/import"),
    ("POST", "/api/honeypots/tokens"),
    ("POST", "/api/honeypots/tokens/t1/trip"),
    ("POST", "/api/honeypots/incidents/i1/summarize"),
    ("POST", "/api/demo/tour/map"),
]


@pytest.mark.parametrize("method,path", VISITOR_REFUSED)
def test_a_visitor_is_refused_what_changes_the_demo(client, method, path):
    for headers in ({}, {"x-amzn-oidc-data": _oidc("someone@example.com")}):
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403 and response.json()["code"] == "demo_admin_only", (method, path, headers)


@pytest.mark.parametrize("method,path", VISITOR_REFUSED + VISITOR_ALLOWED)
def test_the_operator_and_an_admin_token_may_do_everything(client, method, path):
    assert client.request(method, path, headers={"x-amzn-oidc-data": _oidc(ADMIN)}).status_code == 200, "demo.admins, any case"
    assert client.request(method, path, headers={"x-test-token-scope": "admin"}).status_code == 200


@pytest.mark.parametrize("method,path", VISITOR_ALLOWED)
def test_the_tour_stays_open_to_visitors(client, method, path):
    assert client.request(method, path).status_code == 200


def test_an_operate_token_is_not_the_operator(client):
    assert client.post("/api/settings/aws", headers={"x-test-token-scope": "operate"}).status_code == 403


def test_without_a_sign_in_proxy_the_oidc_header_is_not_believed(client):
    extensions.reset_for_tests()  # no identity headers registered: the open edition's own login
    assert client.post("/api/settings/aws", headers={"x-amzn-oidc-data": _oidc(ADMIN)}).status_code == 403


def test_a_call_that_skipped_the_proxy_is_not_believed_either(client):
    # the ALB forwards Bearer calls without Cognito, so their OIDC headers are whatever the caller sent
    assert client.post("/api/settings/aws", headers={"x-amzn-oidc-data": _oidc(ADMIN), "Authorization": "Bearer other"}).status_code == 403


def test_outside_demo_mode_nothing_is_refused(client, monkeypatch):
    monkeypatch.setenv("APP_MODE", "production")
    assert client.post("/api/settings/aws").status_code == 200


def test_the_registered_identity_wins_over_x_user_id():
    from compass_api.middleware.workspace import _identity_from_headers

    extensions.reset_for_tests()
    extensions.register_identity_headers("x-amzn-oidc-identity")
    try:
        request = type("R", (), {"headers": {"x-amzn-oidc-identity": "sub-1", "X-User-Id": "someone-else"}})()
        assert _identity_from_headers(request) == "sub-1"
        request = type("R", (), {"headers": {"X-User-Id": "local"}})()
        assert _identity_from_headers(request) == "local"
    finally:
        extensions.reset_for_tests()
