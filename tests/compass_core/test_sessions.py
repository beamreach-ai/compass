"""The session broker: names, attribution, narrowing, the cache, the context, and the seams."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_core import dispatch, events, sessions

ACCOUNT = "123456789012"
READ = f"arn:aws:iam::{ACCOUNT}:role/service/svc-inventory-reader-k3f9a2"
ACT = f"arn:aws:iam::{ACCOUNT}:role/service/svc-ops-responder-k3f9a2"
CONNECTED = {"aws": {"account_id": ACCOUNT, "region": "us-east-1", "roles": {"read": READ, "act": ACT}, "external_id": "ext-1",
                     "state_buckets": ["tf-state"]}}
NAME_RE = re.compile(r"^[\w+=,.@-]{2,64}$")


class FakeSts:
    def __init__(self, ttl: int = 3600) -> None:
        self.calls = []
        self.ttl = ttl

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{len(self.calls)}",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(timezone.utc) + timedelta(seconds=self.ttl),
            }
        }


@pytest.fixture
def sts(monkeypatch):
    fake = FakeSts()
    monkeypatch.setattr(sessions, "_sts_client", lambda region: fake)
    sessions.clear_cache()
    yield fake
    sessions.clear_cache()


# ── names and attribution ─────────────────────────────────────────────────────

def test_a_read_session_is_named_for_the_captain_and_the_job(sts):
    with sessions.captain_context("mapper"), events.job_event_context("0f3a9c2e-1111-2222-3333-444455556666", "ws"):
        session = sessions.target("read", config=CONNECTED)
    call = sts.calls[0]
    assert call["RoleArn"] == READ
    assert call["RoleSessionName"] == "compass-mapper-0f3a9c2e-1111-2222-3333-444455556666"
    assert call["ExternalId"] == "ext-1"
    assert "SourceIdentity" not in call, "not until the trust policy allows it (aws.source_identity, written by connect)"
    info = sessions.session_info(session)
    assert info.mode == "assume_role" and info.captain == "mapper" and info.capability == "read"
    assert info.name == call["RoleSessionName"] and info.role_arn == READ and info.policy_hash
    assert session.region_name == "us-east-1"


def test_a_read_session_is_narrowed_to_what_the_captain_declared(sts):
    with sessions.captain_context("honeypot"):
        sessions.target("read", config=CONNECTED)
    with sessions.captain_context("librarian"):
        sessions.target("read", config=CONNECTED)
    with sessions.captain_context(sessions.CORE):
        sessions.target("read", config=CONNECTED)
    honeypot, librarian, core = (json.loads(c["Policy"]) for c in sts.calls)
    assert honeypot["Statement"][0]["Action"] == "sts:GetCallerIdentity"
    assert "cloudtrail:LookupEvents" in honeypot["Statement"][1]["Action"] and "ec2:DescribeInstances" not in str(honeypot)
    assert [s["Action"] for s in librarian["Statement"]] == ["sts:GetCallerIdentity"], "declares nothing, reads nothing"
    assert core["Statement"][1]["Resource"] == [READ, ACT], "the core reads the two roles and nothing else"
    assert all(len(c["Policy"]) <= 2048 for c in sts.calls)


def test_a_captain_nobody_knows_reads_nothing_and_an_oversized_block_needs_its_managed_policy(sts, monkeypatch):
    with sessions.captain_context("stranger"):
        sessions.target("read", config=CONNECTED)
    assert [s["Action"] for s in json.loads(sts.calls[0]["Policy"])["Statement"]] == ["sts:GetCallerIdentity"]
    from compass_core import permissions

    monkeypatch.setattr(permissions, "session_policy", lambda *a, **k: None)
    with sessions.captain_context("mapper"), pytest.raises(ValueError, match="too large"):
        sessions.target("read", config=CONNECTED)
    with sessions.captain_context("mapper"):
        sessions.target("read", config={"aws": {**CONNECTED["aws"], "managed_policies": {"mapper": "arn:aws:iam::123456789012:policy/svc-inventory-reader-mapper"}}})
    assert sts.calls[-1]["PolicyArns"] == [{"arn": "arn:aws:iam::123456789012:policy/svc-inventory-reader-mapper"}] and "Policy" not in sts.calls[-1]


def test_source_identity_is_the_requester_once_the_config_allows_it(sts):
    cfg = {"aws": {**CONNECTED["aws"], "source_identity": True}}
    with sessions.captain_context("honeypot", requester="ilia@example.com"):
        sessions.target("read", config=cfg)
    with sessions.captain_context("honeypot"):
        sessions.target("read", config=cfg)
    with sessions.captain_context("honeypot"):
        sessions.target("act", config=cfg, approver="Ilia D (admin)")
    assert [c.get("SourceIdentity") for c in sts.calls] == ["ilia@example.com", "scheduler", "Ilia-D-admin"]


def test_in_request_work_is_named_api_and_a_command_by_hand_cli(sts):
    with sessions.captain_context(sessions.CORE, request_id="deadbeefcafe0000"):
        sessions.target("read", config=CONNECTED)
    with sessions.captain_context("iac"):
        sessions.target("read", config=CONNECTED)
    assert sts.calls[0]["RoleSessionName"] == "compass-core-api-deadbeef"
    assert re.match(r"^compass-iac-cli-[0-9a-f]{8}$", sts.calls[1]["RoleSessionName"])


def test_names_stay_valid_for_long_captains_and_a_custom_prefix():
    name = sessions.session_name("a-captain-with-a-very-long-name-indeed", "0f3a9c2e-1111-2222-3333-444455556666", prefix="svc inventory")
    assert NAME_RE.match(name) and len(name) <= 64
    assert name.startswith("svc-inventory-a-captain-with-a-very-long-nam-"), "the captain is cut at 30 characters"
    assert sessions.session_name("x", "") == "compass-x-x"


def test_a_session_is_never_anonymous():
    with pytest.raises(ValueError, match="no captain"):
        sessions.target("read", config=CONNECTED)
    with pytest.raises(ValueError, match="unknown capability"):
        sessions.target("delete", captain="x", config=CONNECTED)


# ── the cache ─────────────────────────────────────────────────────────────────

def test_read_sessions_are_cached_per_captain_and_job_and_renewed_before_expiry(monkeypatch):
    fake = FakeSts(ttl=3600)
    monkeypatch.setattr(sessions, "_sts_client", lambda region: fake)
    sessions.clear_cache()
    with sessions.captain_context("mapper"), events.job_event_context("job-1", "ws"):
        sessions.target("read", config=CONNECTED)
        sessions.target("read", config=CONNECTED, region="eu-west-1")
    with sessions.captain_context("mapper"), events.job_event_context("job-2", "ws"):
        sessions.target("read", config=CONNECTED)
    assert len(fake.calls) == 2, "one AssumeRole per (captain, job); regions share it"

    soon = FakeSts(ttl=sessions.REFRESH_MARGIN - 1)
    monkeypatch.setattr(sessions, "_sts_client", lambda region: soon)
    sessions.clear_cache()
    with sessions.captain_context("mapper"), events.job_event_context("job-3", "ws"):
        sessions.target("read", config=CONNECTED)
        sessions.target("read", config=CONNECTED)
    assert len(soon.calls) == 2, "a session about to expire is not handed out again"


def test_act_sessions_are_never_cached_and_respond_is_an_alias(sts):
    with sessions.captain_context("honeypot"), events.job_event_context("job-1", "ws"):
        a = sessions.target("act", config=CONNECTED, approver="ilia")
        b = sessions.target("respond", config=CONNECTED, approver="ilia")
    assert len(sts.calls) == 2 and sessions.session_info(a).capability == sessions.session_info(b).capability == "act"
    assert all(c["RoleArn"] == ACT and "Policy" not in c for c in sts.calls), "the act role, not narrowed until a grant names the action"
    with sessions.captain_context("honeypot"):
        assert sessions.target("act", config={"aws": {**CONNECTED["aws"], "roles": {"read": READ}}}) is None, "the act role is explicit: never the read role"


# ── not connected ─────────────────────────────────────────────────────────────

def test_without_connect_there_is_no_target_session(sts):
    with sessions.captain_context("mapper"):
        assert sessions.target("read", config={"aws": {"region": "us-east-1"}}) is None
        assert sessions.target("read", config={"aws": {"auth_mode": "local_iam", "credentials": {"access_key_id": "AKIA", "secret_access_key": "s"}}}) is None, \
            "neither the profile nor a static key reaches a target any more"
        assert sessions.target_env("read", config={"aws": {}}) == {}
    assert sts.calls == []


def test_target_env_freezes_the_credentials_and_unsets_the_profile(sts):
    with sessions.captain_context("iac"):
        env = sessions.target_env("read", config=CONNECTED, region="eu-central-1")
    assert env["AWS_ACCESS_KEY_ID"] == "ASIA1" and env["AWS_SESSION_TOKEN"] == "token" and env["AWS_PROFILE"] is None
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "us-east-1", "the workspace's configured region wins, as before"


def test_with_region_pins_the_region_and_keeps_the_credentials_and_the_tag(sts):
    with sessions.captain_context("mapper"):
        session = sessions.target("read", config=CONNECTED)
    pinned = sessions.with_region(session, "ap-southeast-2")
    assert pinned.region_name == "ap-southeast-2" and pinned.get_credentials().access_key == "ASIA1"
    assert sessions.session_info(pinned) == sessions.session_info(session)


def test_a_host_can_install_its_own_factory():
    seen = []

    def factory(request):
        seen.append(request)
        return "a-session"

    sessions.set_session_factory(factory)
    try:
        with sessions.captain_context("mapper", requester="me"):
            assert sessions.target("read", config={"aws": {}}, region="eu-west-1") == "a-session"
    finally:
        sessions.set_session_factory(None)
    assert seen[0].captain == "mapper" and seen[0].requester == "me" and seen[0].region == "eu-west-1"


# ── the control side ──────────────────────────────────────────────────────────

@mock_aws
def test_control_uses_the_runtime_key_from_the_config_else_the_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("DEFAULT_WORKSPACE", raising=False)
    sessions.clear_cache()
    iam = boto3.client("iam")
    iam.create_user(UserName="svc-compass-runtime-k3f9a2", Path="/service/")
    key = iam.create_access_key(UserName="svc-compass-runtime-k3f9a2")["AccessKey"]
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"workspace_id": "local", "aws": {"control": {"credential_source": "access_key",
                                "credentials": {"access_key_id": key["AccessKeyId"], "secret_access_key": key["SecretAccessKey"]}}}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    from compass_core import config as core_config

    monkeypatch.setattr(core_config, "_loader", None)
    assert sessions.control().client("sts").get_caller_identity()["Arn"].endswith(":user/service/svc-compass-runtime-k3f9a2")
    path.write_text(json.dumps({"workspace_id": "local", "aws": {"control": {"credential_source": "ambient"}}}))
    sessions.clear_cache()
    assert sessions.control().client("sts").get_caller_identity()["Arn"].endswith(":user/moto"), "a machine role: the ambient credentials"
    assert sessions.profile().client("sts").get_caller_identity()["Arn"].endswith(":user/moto"), "connect's profile is always the ambient one"


@mock_aws
def test_the_real_sts_accepts_what_the_broker_sends(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    sessions.clear_cache()
    with sessions.captain_context("mapper", requester="ilia@example.com"):
        session = sessions.target("read", config={"aws": {**CONNECTED["aws"], "source_identity": True}})
    arn = session.client("sts").get_caller_identity()["Arn"]
    assert arn.startswith(f"arn:aws:sts::{ACCOUNT}:assumed-role/svc-inventory-reader-k3f9a2/compass-mapper-cli-")
    assert isinstance(session, boto3.Session)


# ── who sets the context ──────────────────────────────────────────────────────

def test_dispatch_runs_a_handler_as_its_captain_with_the_requester_from_the_payload():
    seen = {}

    def handler(payload):
        seen["captain"] = sessions.current_captain()
        seen["requester"] = sessions.current_requester()
        return {}

    dispatch.register("probe", {"who": handler})
    try:
        dispatch.dispatch("probe", "who", {"user_id": "ilia@example.com"})
    finally:
        dispatch._registry.pop(("probe", "who"), None)
    assert seen == {"captain": "probe", "requester": "ilia@example.com"}
    assert sessions.current_captain() is None, "reset after the handler"


def test_the_api_runs_a_request_as_the_captain_that_owns_the_path():
    from compass_api.middleware.sessions import SessionContextMiddleware

    app = FastAPI()
    app.add_middleware(SessionContextMiddleware)

    @app.get("/api/honeypots/probe")
    @app.get("/api/settings/probe")
    def probe():
        return {"captain": sessions.current_captain(), "request_id": sessions.current_request_id()}

    client = TestClient(app)
    hp = client.get("/api/honeypots/probe").json()
    core = client.get("/api/settings/probe").json()
    assert hp["captain"] == "honeypot" and core["captain"] == sessions.CORE
    assert re.match(r"^[0-9a-f]{32}$", hp["request_id"]) and hp["request_id"] != core["request_id"]
    assert sessions.current_captain() is None
