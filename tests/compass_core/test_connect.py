"""`compass connect` against moto: what it creates, that a second run changes nothing, and
what it writes (docs/specs/access-model.md §4, §6, §11, D1, D7, D8)."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from compass_core import config as core_config
from compass_core import connect as connect_module
from compass_core import sessions
from compass_core.commands import connect

ACCOUNT = "123456789012"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"workspace_id": "local", "aws": {"region": "us-east-1", "install_id": "k3f9a2", "state_buckets": ["tf-state"]},
                                "honeypot": {"alert_queue_arn": f"arn:aws:sqs:us-east-1:{ACCOUNT}:old-queue"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(core_config, "_loader", None)
    monkeypatch.setattr(core_config, "_saver", None)
    monkeypatch.setattr(connect_module, "detect_machine_role", lambda timeout=1.0: None)
    monkeypatch.setattr(connect_module, "ASSUME_RETRY_SECONDS", 2)
    sessions.clear_cache()
    yield path
    sessions.clear_cache()


def _config(path: Path) -> dict:
    return json.loads(path.read_text())


@mock_aws
def test_a_fresh_account_gets_two_roles_a_queue_and_a_runtime_user(workspace):
    out = connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    aws = _config(workspace)["aws"]
    assert aws["roles"] == {"read": f"arn:aws:iam::{ACCOUNT}:role/service/svc-inventory-reader-k3f9a2",
                            "act": f"arn:aws:iam::{ACCOUNT}:role/service/svc-ops-responder-k3f9a2"}
    assert aws["control"]["credential_source"] == "access_key"
    assert aws["control"]["runtime_arn"] == f"arn:aws:iam::{ACCOUNT}:user/service/svc-compass-runtime-k3f9a2"
    assert aws["control"]["credentials"]["access_key_id"].startswith("AKIA") and aws["control"]["credentials"]["secret_access_key"]
    assert aws["source_identity"] is True and aws["external_id"].startswith("beamreach-compass-") and aws["connected_at"]
    assert "auth_mode" not in aws and aws["account_id"] == ACCOUNT and aws["regions"] == ["us-east-1"]
    assert out["aws"]["control"]["credentials"]["secret_access_key"] == "<set>", "never printed"

    iam = boto3.client("iam")
    read = iam.get_role(RoleName="svc-inventory-reader-k3f9a2")["Role"]
    trust = read["AssumeRolePolicyDocument"]
    assert trust["Statement"][0]["Principal"]["AWS"] == aws["control"]["runtime_arn"]
    assert trust["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"] == aws["external_id"]
    assert trust["Statement"][1]["Action"] == "sts:SetSourceIdentity"
    assert {t["Key"]: t["Value"] for t in read["Tags"]} == {"managed-by": "compass"}
    policy = iam.get_role_policy(RoleName="svc-inventory-reader-k3f9a2", PolicyName="inventory-read")["PolicyDocument"]
    sids = [s.get("Sid") for s in policy["Statement"]]
    assert {"CallerIdentity", "SelfAudit", "Inventory", "TerraformState", "DecoyReads"} <= set(sids)
    state = next(s for s in policy["Statement"] if s.get("Sid") == "TerraformState")
    assert state["Resource"] == ["arn:aws:s3:::tf-state", "arn:aws:s3:::tf-state/*"]
    assert iam.list_role_policies(RoleName="svc-ops-responder-k3f9a2")["PolicyNames"] == [], "the act role starts empty"
    assert iam.list_attached_role_policies(RoleName="svc-ops-responder-k3f9a2")["AttachedPolicies"] == []

    runtime = iam.get_user_policy(UserName="svc-compass-runtime-k3f9a2", PolicyName="runtime")["PolicyDocument"]
    assert runtime["Statement"][0]["Action"] == "sts:AssumeRole" and sorted(runtime["Statement"][0]["Resource"]) == sorted(aws["roles"].values())
    assert all(s["Action"] in ("sts:AssumeRole", "sts:GetCallerIdentity") or s["Sid"] == "AlertQueues" for s in runtime["Statement"])

    sqs = boto3.client("sqs")
    queue = aws["control"]["alert_queues"]["us-east-1"]
    attrs = sqs.get_queue_attributes(QueueUrl=queue["url"], AttributeNames=["All"])["Attributes"]
    assert attrs["SqsManagedSseEnabled"] == "true" and "KmsMasterKeyId" not in attrs
    sids = {s["Sid"]: s for s in json.loads(attrs["Policy"])["Statement"]}
    assert sids["TargetAccountDelivery"]["Principal"]["AWS"] == f"arn:aws:iam::{ACCOUNT}:root" and sids["TargetAccountDelivery"]["Action"] == "sqs:SendMessage"
    assert sids["BaseIdentityReads"]["Principal"]["AWS"] == aws["control"]["runtime_arn"]
    from compass_core.aws import alert_queues

    assert alert_queues(_config(workspace)) == {"us-east-1": queue}, "the queue is read from aws.control from here on"
    assert any(s["step"] == "alerts" and "replant-alerts" in s["detail"] and "old-queue" in s["detail"] for s in out["steps"]), "the old queue is named"
    assert alert_queues({"honeypot": {"alert_queue_arn": f"arn:aws:sqs:eu-west-1:{ACCOUNT}:q", "alert_queue_url": "u"}}) == \
        {"eu-west-1": {"arn": f"arn:aws:sqs:eu-west-1:{ACCOUNT}:q", "url": "u"}}, "the old keys are honoured for one release"

    verification = out["verification"]
    assert verification["assumed"]["read"]["ok"] and verification["assumed"]["act"]["ok"]
    assert verification["assumed"]["read"]["arn"].startswith(f"arn:aws:sts::{ACCOUNT}:assumed-role/svc-inventory-reader-k3f9a2/compass-core-connect")
    assert verification["simulated"] is False, "moto has no SimulatePrincipalPolicy; the live checklist covers it"
    assert out["next"][0] == "docker compose up -d"


@mock_aws
def test_a_second_run_changes_nothing_and_keeps_the_key(workspace):
    first = connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    before = _config(workspace)["aws"]
    second = connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    after = _config(workspace)["aws"]
    assert after["control"]["credentials"] == before["control"]["credentials"], "the key is kept when it is still the user's"
    assert after["external_id"] == before["external_id"] and after["connected_at"] == before["connected_at"]
    assert [s["step"] for s in first["steps"] if s.get("created") is True] == ["runtime user", "read role", "act role"]
    assert not any(s.get("created") is True for s in second["steps"])
    assert len(boto3.client("iam").list_access_keys(UserName="svc-compass-runtime-k3f9a2")["AccessKeyMetadata"]) == 1


@mock_aws
def test_the_profile_must_not_be_the_runtime_identity(workspace, monkeypatch):
    connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    creds = _config(workspace)["aws"]["control"]["credentials"]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", creds["access_key_id"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", creds["secret_access_key"])
    with pytest.raises(RuntimeError, match="runtime identity"):
        connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})


@mock_aws
def test_a_machine_role_means_no_runtime_user(workspace, monkeypatch):
    role = f"arn:aws:iam::{ACCOUNT}:role/compass-control-task"
    monkeypatch.setattr(connect_module, "detect_machine_role", lambda timeout=1.0: role)
    out = connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    aws = _config(workspace)["aws"]
    assert aws["control"] == {**aws["control"], "credential_source": "ambient", "runtime_arn": role} and "credentials" not in aws["control"]
    assert boto3.client("iam").list_users(PathPrefix="/service/")["Users"] == []
    trust = boto3.client("iam").get_role(RoleName="svc-inventory-reader-k3f9a2")["Role"]["AssumeRolePolicyDocument"]
    assert trust["Statement"][0]["Principal"]["AWS"] == role
    assert out["verification"]["assumed"] == {"skipped": "the base identity is this machine's role; `serve` checks it at start"}
    given = connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1", "base_identity": f"arn:aws:iam::{ACCOUNT}:role/other"})
    assert given["aws"]["control"]["runtime_arn"] == f"arn:aws:iam::{ACCOUNT}:role/other", "base_identity= overrides the detection"


@mock_aws
def test_rotate_replaces_the_key_and_verify_runs_alone(workspace):
    connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    old = _config(workspace)["aws"]["control"]["credentials"]["access_key_id"]
    rotated = connect({"workspace_id": "local", "verb": "rotate"})
    new = _config(workspace)["aws"]["control"]["credentials"]["access_key_id"]
    assert rotated["replaced"] == old and new != old
    assert [k["AccessKeyId"] for k in boto3.client("iam").list_access_keys(UserName="svc-compass-runtime-k3f9a2")["AccessKeyMetadata"]] == [new]
    verified = connect({"workspace_id": "local", "verb": "verify"})
    assert verified["verification"]["assumed"]["read"]["ok"] and _config(workspace)["aws"]["verified_at"]


@mock_aws
def test_template_renders_the_roles_for_an_account_out_of_reach(workspace):
    out = connect({"workspace_id": "local", "verb": "template", "account": "210987654321", "region": "eu-west-1",
                   "base_identity": f"arn:aws:iam::{ACCOUNT}:user/service/svc-compass-runtime-k3f9a2"})
    import yaml

    template = yaml.safe_load(Path(out["cloudformation"]).read_text())
    assert set(template["Resources"]) == {"ReadRole", "ActRole"}
    read = template["Resources"]["ReadRole"]["Properties"]
    assert read["RoleName"] == "svc-inventory-reader-k3f9a2" and read["Path"] == "/service/"
    assert read["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["AWS"].endswith("svc-compass-runtime-k3f9a2")
    assert read["Policies"][0]["PolicyDocument"]["Statement"][0]["Sid"] == "CallerIdentity"
    assert "Policies" not in template["Resources"]["ActRole"]["Properties"]
    tf = Path(out["terraform"]).read_text()
    assert 'resource "aws_iam_role" "compass_read"' in tf and 'name = "svc-ops-responder-k3f9a2"' in tf
    assert _config(workspace)["aws"]["roles"]["read"] == "arn:aws:iam::210987654321:role/service/svc-inventory-reader-k3f9a2"
    assert out["next"][-1] == "then `compass connect verify`"


# ── the startup check ─────────────────────────────────────────────────────────

@mock_aws
def test_serve_refuses_a_profile_and_a_foreign_identity(workspace, monkeypatch, tmp_path):
    monkeypatch.setenv("COMPASS_RUNTIME_CHECK", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".aws").mkdir()
    monkeypatch.setenv("AWS_PROFILE", "default")
    assert sessions.assert_runtime(_config(workspace)) is None, "before connect an updated install starts with its profile, to run connect"
    monkeypatch.delenv("AWS_PROFILE")
    (tmp_path / "home" / ".aws").rmdir()
    connect({"workspace_id": "local", "account": ACCOUNT, "region": "us-east-1"})
    config = _config(workspace)
    assert sessions.assert_runtime(config) == config["aws"]["control"]["runtime_arn"], "the runtime user's key from the config is the identity"
    monkeypatch.setenv("AWS_PROFILE", "default")
    with pytest.raises(sessions.RuntimeIdentityError, match="AWS_PROFILE"):
        sessions.assert_runtime(config)
    monkeypatch.delenv("AWS_PROFILE")
    (tmp_path / "home" / ".aws").mkdir()
    with pytest.raises(sessions.RuntimeIdentityError, match=r"\.aws is mounted"):
        sessions.assert_runtime(config)
    (tmp_path / "home" / ".aws").rmdir()
    config["aws"]["control"]["credentials"] = {"access_key_id": "testing", "secret_access_key": "testing"}
    workspace.write_text(json.dumps(config))
    sessions.clear_cache()
    with pytest.raises(sessions.RuntimeIdentityError, match="not the runtime identity"):
        sessions.assert_runtime(config)
    monkeypatch.delenv("COMPASS_RUNTIME_CHECK")
    assert sessions.assert_runtime(config) is None, "off outside the image"
    assert sessions.assert_runtime({"aws": {}}) is None


def test_a_role_session_counts_as_the_role():
    assert sessions._same_identity(f"arn:aws:sts::{ACCOUNT}:assumed-role/compass-control-task/i-123", f"arn:aws:iam::{ACCOUNT}:role/service/compass-control-task")
    assert not sessions._same_identity(f"arn:aws:sts::{ACCOUNT}:assumed-role/other/i-123", f"arn:aws:iam::{ACCOUNT}:role/compass-control-task")
    assert not sessions._same_identity("arn:aws:sts::210987654321:assumed-role/compass-control-task/x", f"arn:aws:iam::{ACCOUNT}:role/compass-control-task")


# ── the migration ─────────────────────────────────────────────────────────────

def test_the_migration_turns_assumed_roles_into_roles_and_the_profile_into_nothing(tmp_path, monkeypatch):
    from compass_core.migrations import _access_model

    saved = []
    monkeypatch.setattr(core_config, "save_workspace_config", lambda ws, c: saved.append((ws, c)))
    cfg = {"workspace_id": "w", "aws": {"auth_mode": "assume_role", "region": "us-east-1",
                                        "assume_role": {"account_id": ACCOUNT, "role_name": "BeamreachCompassRole", "external_id": "ext"},
                                        "assume_role_respond": {"account_id": ACCOUNT, "role_name": "svc-ops-responder"}}}
    _access_model(cfg)
    aws = cfg["aws"]
    assert aws["roles"] == {"read": f"arn:aws:iam::{ACCOUNT}:role/BeamreachCompassRole", "act": f"arn:aws:iam::{ACCOUNT}:role/svc-ops-responder"}
    assert aws["external_id"] == "ext" and aws["account_id"] == ACCOUNT and aws["migrated_from"] == "assume_role"
    assert "assume_role" not in aws and "auth_mode" not in aws and saved[0][0] == "w"
    local = {"workspace_id": "w", "aws": {"auth_mode": "local_iam", "region": "us-east-1", "generated_external_id": "gen"}}
    _access_model(local)
    assert local["aws"]["roles"] == {} and local["aws"]["external_id"] == "gen" and local["aws"]["migrated_from"] == "local_iam"
    done = {"workspace_id": "w", "aws": {"roles": {"read": "arn"}}}
    _access_model(done)
    assert len(saved) == 2, "an install that already has roles is left alone"


def test_the_migration_keeps_each_roles_external_id_and_the_targets_region(tmp_path, monkeypatch):
    """A product install whose read and act roles came from different templates: each role keeps
    the external id its trust names, and the target's region survives (the demo's act role trusts
    another id than its read role, and the replay runs in us-east-1 while the control plane is in
    eu-central-1)."""
    from compass_core import aws as core_aws
    from compass_core.migrations import _access_model

    monkeypatch.setattr(core_config, "save_workspace_config", lambda ws, c: None)
    cfg = {"workspace_id": "w", "aws_region": "eu-central-1", "aws": {"auth_mode": "assume_role",
           "assume_role": {"account_id": ACCOUNT, "role_name": "BeamreachCompassRole", "external_id": "read-ext", "region": "us-east-1"},
           "assume_role_respond": {"account_id": ACCOUNT, "role_name": "service/svc-ops-responder", "external_id": "act-ext", "region": "us-east-1"}}}
    _access_model(cfg)
    aws = cfg["aws"]
    assert aws["external_id"] == "read-ext" and aws["external_ids"] == {"act": "act-ext"} and aws["region"] == "us-east-1"
    assert core_aws.external_id(cfg, "read") == "read-ext" and core_aws.external_id(cfg, "act") == "act-ext"
    assert core_aws.external_id(cfg) == "read-ext"
    assert core_aws.external_id({"aws": {"external_id": "one"}}, "act") == "one", "one id for both roles stays the default"
