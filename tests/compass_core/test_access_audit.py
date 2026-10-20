"""The self-audit (docs/specs/access-model.md §9.3, D1): a role with an extra statement yields
one finding; a missing grant statement drops the grant to applied; a role no repository holds
is "not under code"; findings keep their first-seen time and close when gone."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from compass_core import access_audit, grants, permissions
from compass_core import captains as registry
from compass_core.keys import workspace_pk

ACCOUNT = "123456789012"
PK = workspace_pk("ws")
RUNTIME = f"arn:aws:iam::{ACCOUNT}:user/service/svc-compass-runtime-k3f9a2"
CONFIG = {"workspace_id": "ws", "aws": {"account_id": ACCOUNT, "region": "us-east-1", "external_id": "ext",
                                        "roles": {"read": f"arn:aws:iam::{ACCOUNT}:role/service/svc-inventory-reader-k3f9a2",
                                                  "act": f"arn:aws:iam::{ACCOUNT}:role/service/svc-ops-responder-k3f9a2"},
                                        "read_role_name": "svc-inventory-reader-k3f9a2", "act_role_name": "svc-ops-responder-k3f9a2",
                                        "control": {"runtime_arn": RUNTIME}},
          "github": {"iac_repo": "o/r", "token": "t"}}


@pytest.fixture
def world(monkeypatch):
    """moto: the two roles as connect leaves them, and the items table."""
    import importlib

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    importlib.import_module("captains.honeypot.actions")
    registry.discover(refresh=True)
    from compass_core.connect import trust_policy

    with mock_aws():
        iam = boto3.client("iam")
        trust = json.dumps(trust_policy(RUNTIME, "ext"))
        iam.create_role(RoleName="svc-inventory-reader-k3f9a2", Path="/service/", AssumeRolePolicyDocument=trust)
        iam.put_role_policy(RoleName="svc-inventory-reader-k3f9a2", PolicyName="inventory-read",
                            PolicyDocument=json.dumps(permissions.read_role_policy(registry.manifests(), CONFIG)))
        iam.create_role(RoleName="svc-ops-responder-k3f9a2", Path="/service/", AssumeRolePolicyDocument=trust)
        table = boto3.resource("dynamodb").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST",
        )
        yield iam, table


def test_a_clean_install_has_no_findings_and_records_the_run(world):
    iam, table = world
    out = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    assert out["ok"] and out["findings"] == [] and out["under_code"] is True
    assert access_audit.last_run(table, PK)["findings"] == 0 and access_audit.open_findings(table, PK) == []


def test_a_console_change_and_an_attached_policy_are_findings_that_close_when_gone(world):
    iam, table = world
    iam.put_role_policy(RoleName="svc-inventory-reader-k3f9a2", PolicyName="console",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}))
    managed = iam.create_policy(PolicyName="ReadOnlyAccess", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ec2:Describe*", "Resource": "*"}]}))["Policy"]["Arn"]
    iam.attach_role_policy(RoleName="svc-ops-responder-k3f9a2", PolicyArn=managed)
    first = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    kinds = sorted((f["kind"], f["role"]) for f in first["findings"])
    assert kinds == [("unexplained", "act"), ("unexplained", "read")], first["findings"]
    read_finding = next(f for f in first["findings"] if f["role"] == "read")
    assert read_finding["statement"]["Action"] == "s3:GetObject" and read_finding["policy"] == "console" and read_finding["first_seen"]
    assert next(f for f in first["findings"] if f["role"] == "act")["policy_arn"].endswith("ReadOnlyAccess")
    again = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    assert [f["first_seen"] for f in again["findings"]] == [f["first_seen"] for f in first["findings"]], "when did this appear: kept"
    iam.delete_role_policy(RoleName="svc-inventory-reader-k3f9a2", PolicyName="console")
    iam.detach_role_policy(RoleName="svc-ops-responder-k3f9a2", PolicyArn=managed)
    clean = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    assert clean["ok"] and len(clean["closed"]) == 2 and access_audit.open_findings(table, PK) == []
    assert access_audit.last_run(table, PK)["findings"] == 0


def test_a_missing_grant_statement_is_a_finding_and_drops_the_grant_to_applied(world):
    iam, table = world
    row = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope={"user_path_prefixes": ["/service/"]}, mode="autopilot")
    grants.accept(table, PK, row["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    statements = grants.statements_for(row, CONFIG)
    iam.put_role_policy(RoleName="svc-ops-responder-k3f9a2", PolicyName="ops", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))
    grants.refresh(table, PK, CONFIG, iam=iam)
    assert grants.get_grant(table, PK, row["action_id"])["grant_status"] == "armed"
    assert access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)["ok"], "the grant explains the statement"
    iam.delete_role_policy(RoleName="svc-ops-responder-k3f9a2", PolicyName="ops")
    out = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    assert [(f["kind"], f["role"]) for f in out["findings"]] == [("missing", "act")]
    assert grants.get_grant(table, PK, row["action_id"])["grant_status"] == "applied", "says armed but is not in the role"


def test_a_role_no_repository_holds_and_a_foreign_principal_are_findings(world):
    iam, table = world

    class NotFound(Exception):
        status = 404

    def missing():
        raise NotFound()

    out = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=missing)
    assert out["under_code"] is False and sorted(f["role"] for f in out["findings"] if f["kind"] == "not_under_code") == ["act", "read"]
    assert all(f.get("fix") == "pr" for f in out["findings"])
    assert access_audit.under_code({"aws": {}, "github": {}}) is None, "no repository: nothing to be under"
    iam.update_assume_role_policy(RoleName="svc-ops-responder-k3f9a2", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": [RUNTIME, f"arn:aws:iam::{ACCOUNT}:root"]}, "Action": "sts:AssumeRole",
                                                 "Condition": {"StringEquals": {"sts:ExternalId": "ext"}}}]}))
    trust = access_audit.run(CONFIG, workspace_id="ws", table=table, iam=iam, code_probe=lambda: True)
    assert [(f["kind"], f["role"], f.get("principal")) for f in trust["findings"]] == [("trust", "act", f"arn:aws:iam::{ACCOUNT}:root")]


def test_without_roles_the_audit_says_so():
    assert access_audit.run({"aws": {}}, workspace_id="ws", table=object())["error"].startswith("no roles")
