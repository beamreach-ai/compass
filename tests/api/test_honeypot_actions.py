"""HP-27: whitelist rows, catalog, responder-module preview, audits."""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api.middleware.sessions import SessionContextMiddleware
from compass_api.routes import honeypot_actions, honeypots
from compass_core import sessions
from compass_core.keys import honeypot_audit_sort_key, workspace_pk

PK = workspace_pk("ws-a")
SERVICE = "arn:aws:ecs:us-east-1:123456789012:service/replay/web"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
        monkeypatch.setattr(honeypot_actions, "get_actor_ids", lambda request: {"workspace_id": "ws-a", "user_id": "u1"})
        monkeypatch.setattr(
            honeypot_actions,
            "get_workspace_config",
            lambda request: {
                "workspace_id": "ws-a",
                "aws": {"account_id": "123456789012", "region": "us-east-1", "external_id": "ext",
                        "roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-k3f9a2",
                                  "act": "arn:aws:iam::123456789012:role/service/svc-ops-responder-k3f9a2"},
                        "control": {"runtime_arn": "arn:aws:iam::123456789012:user/service/svc-compass-runtime-k3f9a2"}},
                "github": {"iac_repo": "o/r"},
            },
        )
        app = FastAPI()
        app.add_middleware(SessionContextMiddleware)  # the request runs as the honeypot captain, as in the real app
        app.include_router(honeypot_actions.router)
        yield TestClient(app), table


def test_create_list_update_delete(env):
    client, _ = env
    r = client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot", "risk_max": 2})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["action_id"].startswith("act-") and row["enabled"] is True and row["catalog"]["title"]
    assert row["created_by"] == "u1"
    assert row["grant_status"] == "pr_open" and row["armed"] is False, "allowed means accepted: the pull request is queued; it runs once merged and applied"

    listed = client.get("/api/honeypots/actions").json()
    assert [a["action_id"] for a in listed["actions"]] == [row["action_id"]]
    assert {c["action_type"] for c in listed["catalog"]} == {"ecs_restart_task", "iam_deactivate_key"}

    r = client.put(f"/api/honeypots/actions/{row['action_id']}", json={"risk_max": 3})
    assert r.json()["risk_max"] == 3 and r.json()["grant_status"] == "pr_open", "the cap changes in place"
    r = client.put(f"/api/honeypots/actions/{row['action_id']}", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["grant_status"] == "disarmed" and r.json()["mode"] == "off"

    gone = client.delete(f"/api/honeypots/actions/{row['action_id']}").json()
    assert gone["deleted"] is False and gone["disarmed"] == row["action_id"], "the permission is still in AWS: the row stays until the revoke PR is applied"
    assert client.get("/api/honeypots/actions").json()["actions"][0]["grant_status"] == "disarmed"

    proposed = client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "off"}).json()
    assert proposed["grant_status"] == "proposed" and proposed["enabled"] is False
    assert client.delete(f"/api/honeypots/actions/{proposed['action_id']}").json() == {"deleted": proposed["action_id"]}
    assert client.delete(f"/api/honeypots/actions/{proposed['action_id']}").status_code == 404


def test_validation(env):
    client, _ = env
    assert client.post("/api/honeypots/actions", json={"action_type": "nuke", "scope": {}}).status_code == 400
    assert client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {}}).status_code == 400, "scope required"
    assert client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "yolo"}).status_code == 422
    assert client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "risk_max": 9}).status_code == 422


def test_responder_module_preview_reflects_enabled_rows(env):
    client, _ = env
    client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot"})
    client.post("/api/honeypots/actions", json={"action_type": "iam_deactivate_key", "scope": {"user_path_prefixes": ["/service/"]}, "enabled": False})
    module = client.get("/api/honeypots/actions/responder-module").json()
    assert module["role_arn"] == "arn:aws:iam::123456789012:role/service/svc-ops-responder-k3f9a2"
    assert module["enabled_action_types"] == ["ecs_restart_task"]
    actions = {a for s in module["policy"]["Statement"] for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert "ecs:StopTask" in actions and "iam:UpdateAccessKey" not in actions, "a proposed row is not in the module"
    assert "trust" not in module
    assert "services/_ops_access/main.tf" in module["files"]


def test_audits_newest_first(env):
    client, table = env
    for i, ts in enumerate((1700000000000, 1700000001000)):
        table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key(f"{ts}-{i}"), "audit_id": f"{ts}-{i}", "status": "verified"})
    audits = client.get("/api/honeypots/audits?limit=5").json()
    assert [a["audit_id"] for a in audits] == ["1700000001000-1", "1700000000000-0"]
    assert "pk" not in audits[0]


def test_check_reads_the_role_back_and_arms_what_it_holds(env, monkeypatch):
    from compass_core import grants

    client, _ = env
    row = client.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot"}).json()

    class Iam:
        def __init__(self, statements):
            self.statements = statements

        def list_role_policies(self, RoleName):
            return {"PolicyNames": ["ops"] if self.statements else []}

        def get_role_policy(self, RoleName, PolicyName):
            return {"PolicyDocument": {"Statement": self.statements}}

        def list_attached_role_policies(self, RoleName):
            return {"AttachedPolicies": []}

        def simulate_principal_policy(self, **kw):
            return {"EvaluationResults": [{"EvalActionName": a, "EvalDecision": "allowed"} for a in kw["ActionNames"]]}

    monkeypatch.setattr(grants, "_iam_client", lambda config: Iam([]))
    not_yet = client.post("/api/honeypots/actions/check").json()
    assert not_yet["ok"] is False and not_yet["denied"][0]["grant_id"] == row["action_id"] and not_yet["results"] == []
    statements = grants.statements_for(row, honeypot_actions.get_workspace_config(None))
    monkeypatch.setattr(grants, "_iam_client", lambda config: Iam(statements))
    armed = client.post("/api/honeypots/actions/check").json()
    assert armed["ok"] is True and [c["to"] for c in armed["results"]] == ["armed"] and armed["simulated"] is True
    assert client.get("/api/honeypots/actions").json()["actions"][0]["armed"] is True

    monkeypatch.setattr(honeypot_actions, "get_workspace_config", lambda request: {"aws": {"region": "us-east-1"}})
    r = client.post("/api/honeypots/actions/check")
    assert r.status_code == 400 and "aws.roles.act" in r.json()["detail"]


def test_rollback_reactivates_a_deactivated_key_once(env, monkeypatch):
    client, table = env
    with mock_aws():
        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_user(UserName="svc-reporting", Path="/service/")
        key = iam.create_access_key(UserName="svc-reporting")["AccessKey"]["AccessKeyId"]
        iam.update_access_key(UserName="svc-reporting", AccessKeyId=key, Status="Inactive")
        monkeypatch.setattr(sessions, "target", lambda *a, **k: boto3.Session(region_name="us-east-1"))
        audit = {
            "audit_id": "1-abc", "incident_id": "9-inc", "action_type": "iam_deactivate_key", "status": "verified",
            "target": {"region": "us-east-1", "user_arn": "arn:aws:iam::123456789012:user/service/svc-reporting", "access_key_id": key},
            "rollback": {"supported": True, "done_at": None},
        }
        table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key("1-abc"), **audit})
        r = client.post("/api/honeypots/audits/1-abc/rollback", json={"reason": "replay reset"})
        assert r.status_code == 200, r.text
        assert iam.list_access_keys(UserName="svc-reporting")["AccessKeyMetadata"][0]["Status"] == "Active"
        stored = table.get_item(Key={"pk": PK, "sk": honeypot_audit_sort_key("1-abc")})["Item"]
        assert stored["rollback"]["done_at"] and stored["rollback"]["requested_by"] == "u1" and stored["rollback"]["reason"] == "replay reset"
        assert client.post("/api/honeypots/audits/1-abc/rollback").status_code == 409, "only once"


def test_rollback_refuses_actions_that_cannot_be_undone(env):
    client, table = env
    table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key("2-x"), "audit_id": "2-x", "action_type": "ecs_restart_task", "status": "verified"})
    assert client.post("/api/honeypots/audits/2-x/rollback").status_code == 409
    assert client.post("/api/honeypots/audits/nope/rollback").status_code == 404
