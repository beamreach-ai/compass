"""4.4 Settings: integrations (docs/e2e.md)."""

from __future__ import annotations

import json

from e2e import fixture


def test_set_1_connect_left_the_roles_and_a_bad_entry_saves_nothing(stack, page):
    aws = stack.api.get("/api/settings/bootstrap").json()["aws"]
    assert aws["connected"] and aws["account_id"] == fixture.ACCOUNT and aws["mode"] == "roles"
    assert aws["roles"]["read"].startswith(f"arn:aws:iam::{fixture.ACCOUNT}:role/service/svc-inventory-reader-")
    assert aws["runtime_arn"].startswith(f"arn:aws:iam::{fixture.ACCOUNT}:user/service/svc-compass-runtime-")
    assert stack.records["connect_again"]["steps"] and not any(s.get("created") for s in stack.records["connect_again"]["steps"]), "a second connect changes nothing"
    verified = stack.api.post("/api/settings/aws/verify")
    assert verified.status_code == 200 and verified.json()["verification"]["assumed"]["read"]["ok"], verified.text

    refused = stack.api.post("/api/settings/aws/test", json={"mode": "assume_role", "assume_role": {"account_id": "1234", "role_name": "x"}})
    assert refused.status_code == 422
    assert stack.api.get("/api/settings/bootstrap").json()["aws"]["roles"] == aws["roles"], "nothing saved"

    page.open("/workspace?tab=settings")
    page.get_by_text(f"Account {fixture.ACCOUNT} is connected", exact=False).wait_for()


def test_set_2_github_shows_the_token_s_login_and_a_rejected_token_changes_nothing(stack, page):
    github = stack.api.get("/api/settings/bootstrap").json()["github"]
    assert github["connected"] is True and github["login"] == "compass-e2e"
    rejected = stack.api.post("/api/integrations/github/token", json={"token": "bad-token-github-rejects-0000"})
    assert rejected.status_code == 400 and "rejected" in rejected.json()["detail"]
    replaced = stack.api.post("/api/integrations/github/token", json={"token": "e2e-second-token-0123456789"})
    assert replaced.status_code == 200 and replaced.json()["login"] == "compass-e2e"
    page.open("/workspace?tab=settings")
    page.get_by_text("Connected to", exact=False).wait_for()
    page.get_by_text("compass-e2e", exact=True).first.wait_for()


def test_set_3_the_setup_warning_shows_while_github_is_missing(stack, page):
    original = stack.read("config/local.json")
    config = json.loads(original)
    config["github"].pop("token", None)
    config["github"].pop("account_login", None)
    config["github"].pop("account_type", None)
    try:
        stack.write("config/local.json", json.dumps(config))
        page.open("/workspace?tab=settings")
        page.get_by_text("Complete setup to start using Compass", exact=False).wait_for()
    finally:
        stack.write("config/local.json", original)
    page.open("/workspace?tab=settings")
    page.get_by_role("heading", name="Integrations").wait_for()
    assert page.get_by_text("Complete setup to start using Compass", exact=False).count() == 0


def test_set_4_slack_says_it_is_not_configured(page):
    page.open("/workspace?tab=settings")
    page.get_by_text("Slack app not configured for this installation", exact=False).wait_for()


def test_set_5_secrets_never_come_back_from_the_api(stack):
    token = json.loads(stack.read("config/local.json"))["github"]["token"]
    password = stack.credentials()["password"]
    for path in ("/api/settings/bootstrap", "/api/status", "/api/captains", "/api/captains/prs/config", "/api/captains/honeypot/config",
                 "/api/target", "/api/updates"):
        body = stack.api.get(path).text
        assert token not in body, f"the GitHub token in {path}"
        assert password not in body, f"the dashboard password in {path}"
