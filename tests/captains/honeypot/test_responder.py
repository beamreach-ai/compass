"""HP-27: executor audit trail and the generated responder role."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import hcl2

from captains.honeypot.actions.base import ActionContext
from captains.honeypot.responder import AuditLog, rollback_response_action, run_response_action
from captains.honeypot.responder_role import generate_responder_module, responder_policy

SERVICE = "arn:aws:ecs:us-east-1:1:service/web/web"
CLUSTER = "arn:aws:ecs:us-east-1:1:cluster/web"
ROWS = [
    # armed grants: a merged, applied and verified pull request (compass_core.grants); the executor refuses anything else
    {"action_id": "a1", "action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot", "risk_max": 2, "enabled": True, "grant_status": "armed"},
    {"action_id": "a2", "action_type": "iam_deactivate_key", "scope": {"user_path_prefixes": ["/service/"]}, "mode": "autopilot", "risk_max": 2, "enabled": True, "grant_status": "armed"},
    {"action_id": "a3", "action_type": "iam_deactivate_key", "scope": {"user_arns": ["arn:aws:iam::1:user/admin"]}, "mode": "off", "risk_max": 2, "enabled": False},
]


class _Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["sk"]] = Item


class _Ecs:
    def __init__(self, stop_at):
        self.stop_at = stop_at
        self.stopped = []
        self.n = 0

    def describe_services(self, **kw):
        self.n += 1
        return {"services": [{"serviceName": "web", "desiredCount": 1, "runningCount": 1, "taskDefinition": "td", "deployments": [],
                              "events": [{"message": "has reached a steady state.", "createdAt": datetime.now(timezone.utc) + timedelta(seconds=40)}]}]}

    def describe_tasks(self, **kw):
        return {"tasks": [{"taskArn": a, "lastStatus": "RUNNING", "taskDefinitionArn": "td", "containers": [], "attachments": []} for a in kw["tasks"]]}

    def describe_task_definition(self, **kw):
        return {"taskDefinition": {"containerDefinitions": []}}

    def list_tasks(self, **kw):
        return {"taskArns": ["arn:aws:ecs:us-east-1:1:task/web/new"]}

    def stop_task(self, **kw):
        self.stopped.append(kw)
        return {"ResponseMetadata": {"RequestId": "req-1"}}


class _Session:
    def __init__(self, c):
        self.c = c

    def client(self, *_a, **_k):
        return self.c


def _ctx():
    return ActionContext(region="us-east-1", incident_id="inc-xyz", cluster_arn=CLUSTER, service_arn=SERVICE, task_arn="arn:aws:ecs:us-east-1:1:task/web/old")


def test_run_writes_a_complete_audit_row():
    stop_at = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    ecs = _Ecs(stop_at)
    table = _Table()
    t = [stop_at.timestamp()]
    audit = run_response_action(
        session=_Session(ecs), row=ROWS[0], ctx=_ctx(), audit_log=AuditLog(table, "WORKSPACE#ws"),
        chosen_by="agent", risk={"risk": 2}, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0],
    )
    assert audit["status"] == "verified"
    assert ecs.stopped[0]["reason"] == "incident-xyz"
    assert audit["snapshot"]["service"]["name"] == "web"
    assert audit["executed"]["request_id"] == "req-1"
    assert audit["verify"]["ok"] is True
    assert audit["rollback"] == {"supported": False, "done_at": None}
    assert audit["target"]["task_arn"].endswith("/old")
    stored = table.items[f"HONEYPOT#AUDIT#{audit['audit_id']}"]
    assert stored["status"] == "verified" and stored["chosen_by"] == "agent" and stored["risk"] == {"risk": 2}
    assert audit["finished_at"] >= audit["started_at"]


def test_out_of_scope_target_is_refused_before_any_aws_call():
    ecs = _Ecs(datetime.now(timezone.utc))
    table = _Table()
    ctx = _ctx()
    ctx.service_arn = "arn:aws:ecs:us-east-1:1:service/other/api"
    audit = run_response_action(session=_Session(ecs), row=ROWS[0], ctx=ctx, audit_log=AuditLog(table, "pk"), chosen_by="agent")
    assert audit["status"] == "refused" and "scope" in audit["error"]
    assert ecs.stopped == [] and audit["snapshot"] is None


def test_failure_is_recorded_not_raised():
    class Broken(_Ecs):
        def stop_task(self, **kw):
            raise RuntimeError("AccessDenied")

    ecs = Broken(datetime.now(timezone.utc))
    table = _Table()
    audit = run_response_action(session=_Session(ecs), row=ROWS[0], ctx=_ctx(), audit_log=AuditLog(table, "pk"), chosen_by="agent")
    assert audit["status"] == "failed" and "AccessDenied" in audit["error"]
    assert audit["snapshot"] is not None and audit["executed"] is None


def test_rollback_is_refused_for_unsupported_actions():
    table = _Table()
    audit = {"audit_id": "x", "action_type": "ecs_restart_task", "snapshot": {}}
    out = rollback_response_action(session=_Session(_Ecs(datetime.now(timezone.utc))), audit=audit, ctx=_ctx(), audit_log=AuditLog(table, "pk"), requested_by="ilia")
    assert out["rollback"]["supported"] is False


def test_responder_policy_is_the_union_of_enabled_rows():
    policy = responder_policy(ROWS, account_id="1", region="us-east-1")
    actions = {a for s in policy["Statement"] for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert {"ecs:StopTask", "ecs:DescribeServices", "iam:UpdateAccessKey"} <= actions
    assert "ecs:UpdateService" not in actions
    resources = [r for s in policy["Statement"] for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    assert "arn:aws:iam::1:user/admin" not in resources, "disabled rows add nothing"


def test_responder_module_is_valid_hcl_without_product_words():
    module = generate_responder_module(ROWS, account_id="1", region="us-east-1", trust_principal_arns=["arn:aws:iam::2:role/worker"], external_id="ext-1")
    text = module["files"]["services/_responder/main.tf"]
    parsed = hcl2.loads(text)  # must be valid HCL; key quoting differs across hcl2 versions
    assert len(parsed["resource"]) == 2
    assert re.search(r'name\s*=\s*"svc-ops-responder"', text)
    assert "sts:ExternalId" in text and "ext-1" in text
    assert re.search("honeypot|honeytoken|cana" + "ry|beamreach|compass", text, re.I) is None
    assert module["role_arn"] == "arn:aws:iam::1:role/service/svc-ops-responder"
    assert module["enabled_action_types"] == ["ecs_restart_task", "iam_deactivate_key"]
    assert 'module "svc_responder"' in module["root_block"]
