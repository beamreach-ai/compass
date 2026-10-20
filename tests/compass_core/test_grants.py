"""Grants (docs/specs/access-model.md §9, D2, D3): the state machine including widening, the
access module as the union of the right grants, the executor refusing an unarmed row, and the
pull request that regenerates the module."""

from __future__ import annotations

import json
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from compass_core import access_module, access_pr, grants
from compass_core.keys import grant_sort_key, workspace_pk

ACCOUNT = "123456789012"
PK = workspace_pk("ws")
READ = f"arn:aws:iam::{ACCOUNT}:role/service/svc-inventory-reader-k3f9a2"
ACT = f"arn:aws:iam::{ACCOUNT}:role/service/svc-ops-responder-k3f9a2"
CONFIG = {"workspace_id": "ws", "aws": {"account_id": ACCOUNT, "region": "us-east-1", "roles": {"read": READ, "act": ACT}, "external_id": "ext",
                                        "read_role_name": "svc-inventory-reader-k3f9a2", "act_role_name": "svc-ops-responder-k3f9a2",
                                        "control": {"runtime_arn": f"arn:aws:iam::{ACCOUNT}:user/service/svc-compass-runtime-k3f9a2"}},
          "github": {"iac_repo": "o/r", "token": "t"}, "iac": {"terraform_root": "envs/prod"}}
SCOPE = {"user_path_prefixes": ["/service/"]}


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    import importlib

    importlib.import_module("captains.honeypot.actions")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST",
        )


class FakeIam:
    """The act role as IAM would report it, and a simulation that allows what is held."""

    def __init__(self, statements=None, simulate=True):
        self.statements = list(statements or [])
        self.simulate_available = simulate

    def list_role_policies(self, RoleName):
        return {"PolicyNames": ["ops"] if self.statements else []}

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": {"Version": "2012-10-17", "Statement": self.statements}}

    def list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": []}

    def simulate_principal_policy(self, **kw):
        if not self.simulate_available:
            raise NotImplementedError("no simulation here")
        held = {a for s in self.statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
        return {"EvaluationResults": [{"EvalActionName": a, "EvalDecision": "allowed" if a in held else "implicitDeny"} for a in kw["ActionNames"]]}


class FakeRegistry:
    def __init__(self, merged=()):
        self.merged = set(merged)

    def get_by_url(self, url):
        return {"state": "merged" if url in self.merged else "open"}


def test_the_lifecycle_from_proposed_to_armed_and_back(table):
    queued = []
    enqueue = lambda **kw: queued.append(kw) or {"job_id": "j1"}  # noqa: E731
    row = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope=SCOPE, mode="autopilot", actor="ilia@example.com")
    assert row["grant_status"] == "proposed" and row["sk"] == grant_sort_key("honeypot", row["action_id"]) and row["enabled"]
    with pytest.raises(grants.GrantError, match="not an action mapper registers"):
        grants.propose(table, PK, captain="mapper", action_type="iam_deactivate_key", scope=SCOPE)

    accepted = grants.accept(table, PK, row["action_id"], config=CONFIG, actor="ilia@example.com", enqueue=enqueue)
    assert accepted["grant_status"] == "pr_open" and accepted["policy_hash"] and queued[0]["command"] == "access" and queued[0]["payload"]["verb"] == "pr"
    assert not grants.is_armed(accepted), "pr_open cannot run"
    with pytest.raises(grants.GrantError, match="not armed"):
        grants.narrowing(accepted, CONFIG)

    # the PR exists; the row records it (what access_pr does after opening)
    accepted["grant_pr"] = "https://github.com/o/r/pull/7"
    grants._put(table, accepted)
    statements = grants.statements_for(accepted, CONFIG)

    # merged but not applied: the role still holds nothing
    nothing = grants.refresh(table, PK, CONFIG, iam=FakeIam([]), registry=FakeRegistry(merged={"https://github.com/o/r/pull/7"}))
    assert [c["to"] for c in nothing["changed"]] == ["merged"], "a merge is never taken as an apply (D2)"
    assert grants.get_grant(table, PK, row["action_id"])["grant_status"] == "merged"

    # applied: IAM holds the statements, and allows them
    armed = grants.refresh(table, PK, CONFIG, iam=FakeIam(statements), registry=FakeRegistry(merged={"https://github.com/o/r/pull/7"}))
    assert [c["to"] for c in armed["changed"]] == ["armed"] and armed["simulated"] is True
    current = grants.get_grant(table, PK, row["action_id"])
    assert current["grant_status"] == "armed" and current["applied_at"] and current["verified_at"] and grants.is_armed(current)
    assert [h["to"] for h in current["grant_history"]] == ["proposed", "pr_open", "merged", "armed"]
    narrowing = grants.narrowing(current, CONFIG)
    assert narrowing.statements == statements and narrowing.action_type == "iam_deactivate_key"

    # a person removes it: disarmed at once, the revoke PR queued, the permission still in IAM
    removed = grants.remove(table, PK, row["action_id"], config=CONFIG, actor="ilia@example.com", enqueue=enqueue)
    assert removed["grant_status"] == "disarmed" and not grants.is_armed(removed) and removed["mode"] == "off" and queued[-1]["payload"]["reason"].startswith("revoke")
    still = grants.refresh(table, PK, CONFIG, iam=FakeIam(statements), registry=FakeRegistry())
    assert still["changed"] == [], "removal is not done while IAM holds the statements"
    gone = grants.refresh(table, PK, CONFIG, iam=FakeIam([]), registry=FakeRegistry(merged={"https://github.com/o/r/pull/7"}))
    assert [c["to"] for c in gone["changed"]] == ["revoked"]
    assert grants.summary(table, PK) == {"total": 1, "armed": 0, "in_flight": 0, "waiting_on": []}


def test_decline_set_mode_and_the_rules(table):
    queued = []
    enqueue = lambda **kw: queued.append(kw)  # noqa: E731
    row = grants.propose(table, PK, captain="honeypot", action_type="ecs_restart_task", scope={"service_arns": [f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/web/web"]})
    declined = grants.decline(table, PK, row["action_id"], actor="x")
    assert declined["grant_status"] == "declined" and not declined["enabled"]
    with pytest.raises(grants.GrantError, match="only a proposed grant"):
        grants.decline(table, PK, row["action_id"])
    back = grants.set_mode(table, PK, row["action_id"], config=CONFIG, mode="approve", enqueue=enqueue)
    assert back["grant_status"] == "pr_open" and back["mode"] == "approve" and len(queued) == 1
    risk = grants.set_mode(table, PK, row["action_id"], config=CONFIG, risk_max=4, enqueue=enqueue)
    assert risk["risk_max"] == 4 and risk["grant_status"] == "pr_open" and len(queued) == 1, "the cap changes in place"
    off = grants.set_mode(table, PK, row["action_id"], config=CONFIG, mode="off", enqueue=enqueue)
    assert off["grant_status"] == "disarmed" and len(queued) == 2
    with pytest.raises(grants.GrantError, match="allows nothing"):
        empty = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope={})
        grants.accept(table, PK, empty["action_id"], config=CONFIG, enqueue=enqueue)
    with pytest.raises(grants.GrantError, match="approve or autopilot"):
        grants.accept(table, PK, grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope=SCOPE)["action_id"], config=CONFIG, mode="off")


def test_widening_keeps_the_old_grant_armed_until_the_new_one_is(table):
    enqueue = lambda **kw: None  # noqa: E731
    old = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope={"user_path_prefixes": ["/service/"]}, mode="autopilot")
    grants.accept(table, PK, old["action_id"], config=CONFIG, enqueue=enqueue)
    old_statements = grants.statements_for(old, CONFIG)
    grants.refresh(table, PK, CONFIG, iam=FakeIam(old_statements), registry=FakeRegistry())
    assert grants.get_grant(table, PK, old["action_id"])["grant_status"] == "armed"

    new = grants.widen(table, PK, old["action_id"], scope={"user_path_prefixes": ["/service/", "/ops/"]}, config=CONFIG)
    assert new["grant_status"] == "proposed" and new["supersedes"] == old["action_id"]
    assert grants.get_grant(table, PK, old["action_id"])["grant_status"] == "armed", "widening never disarms what works"
    grants.accept(table, PK, new["action_id"], config=CONFIG, enqueue=enqueue)
    both = old_statements + grants.statements_for(new, CONFIG)
    result = grants.refresh(table, PK, CONFIG, iam=FakeIam(both), registry=FakeRegistry())
    assert {c["to"] for c in result["changed"]} == {"armed"}
    assert grants.get_grant(table, PK, new["action_id"])["grant_status"] == "armed"
    assert grants.get_grant(table, PK, old["action_id"])["grant_status"] == "disarmed", "the old one is retired once the new one is armed"


def test_an_armed_grant_missing_from_the_role_drops_to_applied_and_simulation_may_be_unavailable(table):
    row = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope=SCOPE, mode="autopilot")
    grants.accept(table, PK, row["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    statements = grants.statements_for(row, CONFIG)
    moto_like = grants.refresh(table, PK, CONFIG, iam=FakeIam(statements, simulate=False), registry=FakeRegistry())
    assert moto_like["simulated"] is False and [c["to"] for c in moto_like["changed"]] == ["armed"]
    assert grants.get_grant(table, PK, row["action_id"])["verification"]["simulated"] is False, "armed, and says the simulation was not available"
    dropped = grants.refresh(table, PK, CONFIG, iam=FakeIam([]), registry=FakeRegistry())
    assert [(c["from"], c["to"]) for c in dropped["changed"]] == [("armed", "applied")], "the self-audit's case"


def test_the_access_module_is_the_union_of_the_right_grants(table):
    a = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope=SCOPE, mode="autopilot")
    grants.propose(table, PK, captain="honeypot", action_type="ecs_restart_task", scope={"service_arns": [f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/web/web"]})
    c = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope={"user_arns": [f"arn:aws:iam::{ACCOUNT}:user/svc-x"]})
    grants.accept(table, PK, a["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    grants.accept(table, PK, c["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    grants.remove(table, PK, c["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    rendered = access_module.render(CONFIG, grants.list_grants(table, PK), with_imports=True)
    policy = rendered["act_policy"]
    actions = {a for s in policy["Statement"] for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert "iam:UpdateAccessKey" in actions and "ecs:StopTask" not in actions, "proposed (b) is not in the module, disarmed (c) is out"
    assert all("svc-x" not in json.dumps(s) for s in policy["Statement"])
    main_tf = rendered["files"]["services/_ops_access/main.tf"]
    assert 'name = "svc-inventory-reader-k3f9a2"' in main_tf and 'name = "svc-ops-responder-k3f9a2"' in main_tf
    assert '"sts:SetSourceIdentity"' in main_tf and "svc-compass-runtime-k3f9a2" in main_tf and '"ops"' in main_tf
    assert "import {" in rendered["files"]["services/_ops_access/imports.tf"]
    assert 'module "svc_ops_access"' in rendered["root"] and rendered["read_policy"]["Statement"][0]["Sid"] == "CallerIdentity"
    empty = access_module.render(CONFIG, [])
    assert '"ops"' not in empty["files"]["services/_ops_access/main.tf"], "no grants, no act policy"


def test_the_pull_request_regenerates_the_module_and_records_its_url(table, monkeypatch):
    class Repo:
        def __init__(self):
            self.files = {}
            self.prs = []

        def get_branch(self, name):
            return SimpleNamespace(commit=SimpleNamespace(sha="base"))

        def create_git_ref(self, ref, sha):
            pass

        def get_contents(self, path, ref=None):
            from github import GithubException

            if path in self.files:
                return SimpleNamespace(path=path, sha="s", decoded_content=self.files[path].encode(), type="file")
            raise GithubException(404, {"message": "Not Found"}, {})

        def create_file(self, path, msg, content, branch=None):
            self.files[path] = content

        def update_file(self, path, msg, content, sha, branch=None):
            self.files[path] = content

        def create_pull(self, title, body, head, base):
            self.prs.append({"title": title, "body": body, "head": head, "base": base})
            return SimpleNamespace(html_url="https://github.com/o/r/pull/9")

    repo = Repo()
    monkeypatch.setattr(access_pr, "resolve_github_token", lambda *a, **k: "t")
    monkeypatch.setattr(access_pr, "github_client", lambda token: SimpleNamespace(get_repo=lambda name: repo))
    recorded = []
    monkeypatch.setattr(access_pr.PRRegistry, "from_config", classmethod(lambda cls, config, workspace_id=None: SimpleNamespace(record=lambda **kw: recorded.append(kw))))
    a = grants.propose(table, PK, captain="honeypot", action_type="iam_deactivate_key", scope=SCOPE, mode="autopilot", actor="ilia@example.com")
    grants.accept(table, PK, a["action_id"], config=CONFIG, actor="ilia@example.com", enqueue=lambda **kw: None)
    b = grants.propose(table, PK, captain="honeypot", action_type="ecs_restart_task", scope={"service_arns": [f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/web/web"]})
    grants.accept(table, PK, b["action_id"], config=CONFIG, enqueue=lambda **kw: None)
    grants.remove(table, PK, b["action_id"], config=CONFIG, actor="ilia@example.com", enqueue=lambda **kw: None)

    out = access_pr.open_pr(CONFIG, workspace_id="ws", actor="ilia@example.com", reason="grant iam_deactivate_key", table=table)
    assert out["pr_url"] == "https://github.com/o/r/pull/9" and out["imports"] is True and out["grants"] == 1 and out["revocations"] == 1
    assert set(repo.files) == {"envs/prod/services/_ops_access/main.tf", "envs/prod/services/_ops_access/imports.tf", "envs/prod/access.tf"}
    assert repo.prs[0]["head"] == "beamreach/access" and "iam:UpdateAccessKey" in repo.prs[0]["body"] and "ilia@example.com" in repo.prs[0]["body"]
    assert "Removed from the act role" in repo.prs[0]["body"]
    rows = {r["action_id"]: r for r in grants.list_grants(table, PK)}
    assert rows[a["action_id"]]["grant_pr"] == "https://github.com/o/r/pull/9" and rows[a["action_id"]]["grant_status"] == "pr_open"
    assert rows[b["action_id"]]["grant_status"] == "revoke_pr_open" and rows[b["action_id"]]["grant_pr"] == "https://github.com/o/r/pull/9"
    assert recorded[0]["origin"]["captain"] == "core" and recorded[0]["pr_type"] == "access"
    again = access_pr.open_pr(CONFIG, workspace_id="ws", table=table)
    assert again["imports"] is False, "the module exists on the base branch now: no import blocks"
    with pytest.raises(RuntimeError, match="no IaC repository"):
        access_pr.open_pr({**CONFIG, "github": {}}, workspace_id="ws", table=table)


def test_the_executor_refuses_an_unarmed_row_before_any_aws_call():
    from captains.honeypot.actions.base import ActionContext
    from captains.honeypot.responder import run_response_action

    class Log:
        def __init__(self):
            self.rows = []

        def write(self, audit):
            self.rows.append(dict(audit))

    class NoAws:
        def client(self, *a, **k):
            raise AssertionError("no AWS call may happen")

    log = Log()
    ctx = ActionContext(region="us-east-1", incident_id="1-abc", user_arn=f"arn:aws:iam::{ACCOUNT}:user/service/svc-x", user_name="svc-x", access_key_id="AKIA")
    row = {"action_id": "a", "action_type": "iam_deactivate_key", "scope": SCOPE, "mode": "autopilot", "risk_max": 2, "enabled": True, "grant_status": "pr_open"}
    audit = run_response_action(session=NoAws(), row=row, ctx=ctx, audit_log=log, chosen_by="agent")
    assert audit["status"] == "refused" and audit["error"].startswith("wait_for_grant") and len(log.rows) == 1
