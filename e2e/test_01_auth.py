"""4.2 Sign-in, sessions and API tokens (docs/e2e.md)."""

from __future__ import annotations

import httpx
import pytest

PROTECTED = ["/api/settings/bootstrap", "/api/status", "/api/jobs", "/api/captains", "/api/maps", "/api/honeypots/tokens",
             "/api/prs", "/api/updates", "/api/target", "/api/notifications/endpoints"]


def test_aut_1_every_api_route_needs_a_session_or_a_token_and_health_does_not(stack):
    anonymous = stack.anonymous()
    for path in PROTECTED:
        assert anonymous.get(path).status_code == 401, path
    assert anonymous.post("/api/prs/sync").status_code == 401
    assert anonymous.get("/health").status_code == 200
    assert anonymous.get("/api/auth/me").status_code in (200, 401)  # public: says who you are, or that you are no one


def test_aut_2_signing_in_and_out_in_the_browser(anonymous_page, stack):
    page = anonymous_page
    page.open("/workspace?tab=queue")
    page.wait_for_url("**/login**")
    credentials = stack.credentials()
    page.get_by_label("Username", exact=False).fill(credentials["username"])
    page.get_by_label("Password", exact=False).fill(credentials["password"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/workspace**")
    page.get_by_role("link", name="Queue").first.wait_for()
    page.get_by_role("button", name="Logout").click()
    page.wait_for_url("**/login**")
    page.open("/workspace?tab=queue")
    page.wait_for_url("**/login**")


def _token(stack, scope: str) -> dict:
    created = stack.api.post("/api/auth/tokens", json={"name": f"e2e {scope}", "scope": scope})
    assert created.status_code == 201, created.text
    return created.json()


def _as(stack, token: dict) -> httpx.Client:
    return httpx.Client(base_url=stack.web, headers={"Authorization": f"Bearer {token['token']}"}, timeout=30)


def test_aut_3_each_token_scope_can_do_what_it_allows_and_nothing_more(stack):
    read, operate, admin = (_as(stack, _token(stack, scope)) for scope in ("read", "operate", "admin"))

    # read: every GET, no write
    assert read.get("/api/status").status_code == 200
    assert read.post("/api/prs/sync").status_code == 403
    assert read.get("/api/auth/tokens").status_code == 403, "listing tokens is admin"

    # operate: start work, but not change how Compass is configured
    assert operate.post("/api/prs/sync").status_code in (200, 202)
    assert operate.put("/api/updates/policy", json={"auto": "notify"}).status_code == 403
    assert operate.post("/api/captains/install", json={"name": "hello"}).status_code == 403
    assert operate.put("/api/prs/watcher", json={}).status_code == 403

    # admin: configuration too
    assert admin.put("/api/updates/policy", json={"auto": "notify"}).status_code == 200
    assert admin.get("/api/auth/tokens").status_code == 200


def test_aut_4_a_revoked_token_is_refused_at_once(stack):
    token = _token(stack, "read")
    client = _as(stack, token)
    assert client.get("/api/status").status_code == 200
    assert stack.api.delete(f"/api/auth/tokens/{token['token_id']}").status_code == 200
    assert client.get("/api/status").status_code == 401


@pytest.fixture
def read_only(stack):
    stack.env(READ_ONLY="1")
    stack.restart("api")
    yield
    stack.env(READ_ONLY=None)
    stack.restart("api")


def test_aut_5_a_read_only_install_refuses_every_write_and_says_so(read_only, stack, page):
    assert stack.api.post("/api/prs/sync").status_code == 403
    assert stack.api.put("/api/updates/policy", json={"auto": "notify"}).status_code == 403
    assert stack.api.post("/api/captains/mapper/disable").status_code == 403
    assert stack.api.get("/api/status").json()["app"]["read_only"] is True
    page.open("/workspace?tab=maps")
    page.get_by_text("read-only", exact=False).first.wait_for()
