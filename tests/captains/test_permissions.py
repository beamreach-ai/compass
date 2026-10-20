"""Every first-party captain declares what it reads, the declaration validates, and the
code calls only what it declares (docs/specs/access-model.md §5, §8; the check that keeps
the spec's Appendix A true)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compass_core import captains, permissions
from compass_core.permissions import AwsPermissions, Permissions, Statement

ROOT = Path(__file__).resolve().parents[2]
CONFIG = {"aws": {"account_id": "123456789012", "state_buckets": ["example-state"],
                  "roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-k3f9a2",
                            "act": "arn:aws:iam::123456789012:role/service/svc-ops-responder-k3f9a2"}}}


@pytest.fixture(scope="module")
def manifests():
    captains.discover(refresh=True)
    return {m.name: m for m in captains.manifests()}


def test_every_shipped_captain_declares_permissions(manifests):
    for name, m in manifests.items():
        assert m.permissions is not None, f"{name} declares no permissions"
        assert permissions.permission_errors(m.permissions) == [], name


@pytest.mark.parametrize("name", ["mapper", "honeypot", "iac"])
def test_the_code_calls_only_what_the_manifest_declares(manifests, name):
    calls = permissions.aws_calls(ROOT / "captains" / name)
    missing = permissions.uncovered(manifests[name].permissions, calls)
    assert not missing, "declare or remove: " + ", ".join(str(c) for c in missing)


def test_the_static_pass_sees_what_the_captains_do():
    mapper = {c.action for c in permissions.aws_calls(ROOT / "captains" / "mapper") if not c.control}
    assert {"ec2:DescribeInstances", "lambda:ListFunctions", "s3:GetObject", "s3:ListAllMyBuckets", "states:ListStateMachines",
            "elasticloadbalancing:DescribeLoadBalancers", "cloudwatch:DescribeAlarms", "secretsmanager:ListSecrets"} <= mapper
    assert "lambda:GetFunction" not in mapper
    honeypot = permissions.aws_calls(ROOT / "captains" / "honeypot")
    assert {c.action for c in honeypot if c.control} == {"sqs:ReceiveMessage", "sqs:DeleteMessage", "ses:SendEmail"}, "the control side is not the target"
    assert "tag:GetResources" in {c.action for c in honeypot}, "resourcegroupstaggingapi maps to the tag: prefix"
    assert not [c for c in honeypot if "actions/" in c.file], "the write actions are checked against their own statements"


def test_the_static_pass_on_a_sample(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "work.py").write_text(
        "from compass_core import sessions\n"
        "def a(session):\n"
        "    client = session.client('lambda')\n"
        "    client.list_functions()\n"
        "    for page in client.get_paginator('list_event_source_mappings').paginate(): pass\n"
        "def b(session, s3_client):\n"
        "    client = session.client('dynamodb')\n"
        "    client.list_tables()\n"
        "    s3_client.get_object(Bucket='b', Key='k')\n"
        "    sessions.control(region='x').client('ses').send_email()\n"
        "    session.client('s3').list_buckets()\n"
        "class C:\n"
        "    def __init__(self, session): self._sqs = sessions.control().client('sqs')\n"
        "    def run(self): self._sqs.delete_message()\n"
    )
    calls = permissions.aws_calls(tmp_path / "pkg")
    assert sorted((c.action, c.control) for c in calls) == sorted([
        ("lambda:ListFunctions", False), ("lambda:ListEventSourceMappings", False), ("dynamodb:ListTables", False),
        ("s3:GetObject", False), ("ses:SendEmail", True), ("s3:ListAllMyBuckets", False), ("sqs:DeleteMessage", True),
    ])
    declared = Permissions(aws=AwsPermissions(read=(Statement(actions=("lambda:List*", "dynamodb:ListTables", "s3:ListAllMyBuckets")),)))
    assert [c.action for c in permissions.uncovered(declared, calls)] == ["s3:GetObject"]


def test_a_write_verb_or_an_open_state_read_is_refused():
    bad = Permissions(aws=AwsPermissions(read=(
        Statement(actions=("iam:UpdateAccessKey",)),
        Statement(actions=("s3:GetObject",), resources=("*",)),
        Statement(actions=("lambda:Get*",)),
        Statement(actions=("*:*",)),
        Statement(actions=("ecs:*",)),
        Statement(actions=("ecs",)),
    )), github=("repo",), egress=("not a host",))
    problems = permissions.permission_errors(bad)
    assert any("iam:UpdateAccessKey is not a read-only verb" in p for p in problems)
    assert any("s3:GetObject on *" in p for p in problems)
    assert any("lambda:Get* is never a read" in p for p in problems)
    assert any("*:* is never a read" in p for p in problems)
    assert any("ecs:* is not a read-only verb" in p for p in problems)
    assert any("'ecs' is not <service>:<Action>" in p for p in problems)
    assert any("github scope 'repo'" in p for p in problems) and any("egress 'not a host'" in p for p in problems)
    assert permissions.permission_errors(None) == [] and permissions.permission_errors(Permissions()) == []


def test_rendering_fills_placeholders_and_drops_what_it_cannot(manifests):
    rendered = permissions.captain_statements(manifests["mapper"].permissions, CONFIG)
    state = [s for s in rendered if s.get("Sid") == "TerraformState"]
    assert state and state[0]["Resource"] == ["arn:aws:s3:::example-state", "arn:aws:s3:::example-state/*"]
    without = permissions.captain_statements(manifests["mapper"].permissions, {"aws": {}})
    assert not [s for s in without if s.get("Sid") == "TerraformState"], "no bucket configured, no bucket statement"
    core = permissions.captain_statements(manifests["core"].permissions, CONFIG)
    assert core[0]["Resource"] == [CONFIG["aws"]["roles"]["read"], CONFIG["aws"]["roles"]["act"]]
    assert permissions.captain_statements(manifests["core"].permissions, {"aws": {}}) == [], "no roles yet, no self-audit block"


def test_the_session_policy_fits_inline_and_the_read_role_is_the_union(manifests):
    for name in ("mapper", "honeypot", "iac", "core"):
        policy = permissions.session_policy(manifests[name].permissions, CONFIG)
        assert policy is not None and len(policy) <= permissions.SESSION_POLICY_LIMIT, name
        assert json.loads(policy)["Statement"][0]["Action"] == "sts:GetCallerIdentity"
    nothing = json.loads(permissions.session_policy(manifests["librarian"].permissions, CONFIG))
    assert [s["Action"] for s in nothing["Statement"]] == ["sts:GetCallerIdentity"], "a captain that declares nothing reads nothing"
    role = permissions.read_role_policy(manifests.values(), CONFIG)
    sids = [s.get("Sid") for s in role["Statement"]]
    assert sids[0] == "CallerIdentity" and {"SelfAudit", "Inventory", "TerraformState", "DecoyReads"} <= set(sids)
    assert len(sids) == len(set(json.dumps(s, sort_keys=True) for s in role["Statement"])), "no statement twice (iac shares the mapper's)"
    assert permissions.covers(["ecs:Describe*", "iam:Get*Policy*"], "ecs:DescribeServices")
    assert permissions.covers(["iam:Get*Policy*"], "iam:GetPolicyVersion") and not permissions.covers(["iam:Get*Policy*"], "iam:GetUser")


def test_describe_groups_reads_by_service(manifests):
    shown = permissions.describe(manifests["honeypot"].permissions)
    assert shown["declared"] and shown["reads"]["cloudtrail"] == ["DescribeTrails", "GetTrailStatus", "LookupEvents"]
    assert shown["github"] == ["contents:write", "pull_requests:write"] and shown["egress"] == ["api.github.com"]
    assert permissions.describe(None) == {"declared": False, "reads": {}, "actions": [], "github": [], "egress": []}
