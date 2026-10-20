"""HP-10/HP-11: choose among whitelisted rows, gate, run, record, approve."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from captains.honeypot import incidents
from captains.honeypot.choose import applicable_rows, choose_actions
from captains.honeypot.respond import approve_pending_action, context_from_incident, respond_to_incident
from compass_core.keys import honeypot_action_sort_key

CLUSTER = "arn:aws:ecs:us-east-1:1:cluster/web"
SERVICE = "arn:aws:ecs:us-east-1:1:service/web/web"
TASK = "arn:aws:ecs:us-east-1:1:task/web/old"
PK = "WORKSPACE#ws"

ROW_RESTART = {"action_id": "a-restart", "action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot", "risk_max": 2, "enabled": True}
ROW_KEY = {"action_id": "a-key", "action_type": "iam_deactivate_key", "scope": {"user_path_prefixes": ["/service/"]}, "mode": "autopilot", "risk_max": 2, "enabled": True}
ROW_OTHER = {"action_id": "a-other", "action_type": "ecs_restart_task", "scope": {"service_arns": ["arn:aws:ecs:us-east-1:1:service/other/api"]}, "mode": "autopilot", "risk_max": 2, "enabled": True}

INCIDENT = {
    "incident_id": "1-abc", "status": "open", "source_ip": "10.42.1.49",
    "placement": {"service_arn": SERVICE, "cluster_arn": CLUSTER, "container": "web"},
    "correlation": {"service_arn": SERVICE, "task": {"task_arn": TASK, "task_id": "old", "cluster_arn": CLUSTER, "containers": ["web"]}, "task_role_arn": "arn:aws:iam::1:role/web-task"},
}
ALERTS = [{"alert_id": "x", "principal_arn": "arn:aws:iam::1:user/service/svc-reporting", "access_key_id": "AKIA1"}]


class _Table:
    def __init__(self, rows):
        self.items = {honeypot_action_sort_key(r["action_id"]): {"pk": PK, "sk": honeypot_action_sort_key(r["action_id"]), **r} for r in rows}
        self.audits = {}

    def query(self, **kw):
        return {"Items": [i for k, i in self.items.items() if k.startswith("HONEYPOT#ACTION#")]}

    def put_item(self, Item):
        (self.audits if Item["sk"].startswith("HONEYPOT#AUDIT#") else self.items)[Item["sk"]] = dict(Item)

    def get_item(self, Key):
        return {"Item": self.items[Key["sk"]]} if Key["sk"] in self.items else {}


class _Ecs:
    def __init__(self):
        self.stopped = []

    def describe_services(self, **kw):
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
        return {"ResponseMetadata": {"RequestId": "r"}}


class _Iam:
    def __init__(self):
        self.status = "Active"

    def list_access_keys(self, UserName):
        return {"AccessKeyMetadata": [{"AccessKeyId": "AKIA1", "Status": self.status}]}

    def update_access_key(self, UserName, AccessKeyId, Status):
        self.status = Status
        return {"ResponseMetadata": {"RequestId": "r"}}


class _Session:
    def __init__(self):
        self.ecs, self.iam = _Ecs(), _Iam()

    def client(self, name, **kw):
        return {"ecs": self.ecs, "iam": self.iam}[name]


@pytest.fixture
def env(monkeypatch):
    marked = {}
    monkeypatch.setattr(incidents, "get_incident", lambda t, p, i: dict(INCIDENT, **marked) if i == "1-abc" else None)
    monkeypatch.setattr(incidents, "alerts_for_incident", lambda t, p, i: ALERTS)
    monkeypatch.setattr(incidents, "mark", lambda t, p, i, **f: marked.update(f))
    return marked


def test_context_from_incident_uses_correlation_and_alert_principal():
    ctx = context_from_incident(INCIDENT, ALERTS, region="us-east-1")
    assert ctx.task_arn == TASK and ctx.service_arn == SERVICE and ctx.cluster_arn == CLUSTER
    assert ctx.user_name == "svc-reporting" and ctx.user_arn.endswith("user/service/svc-reporting") and ctx.access_key_id == "AKIA1"


def test_applicable_rows_filters_by_scope_mode_and_enabled():
    ctx = context_from_incident(INCIDENT, ALERTS, region="us-east-1")
    rows = [ROW_OTHER, ROW_KEY, ROW_RESTART, dict(ROW_RESTART, action_id="off", mode="off"), dict(ROW_KEY, action_id="dis", enabled=False)]
    assert [r["action_id"] for r in applicable_rows(rows, ctx)] == ["a-restart", "a-key"], "catalog order, scoped, enabled, not off"


def test_choose_is_deterministic_without_a_model_and_validates_model_answers():
    ctx = context_from_incident(INCIDENT, ALERTS, region="us-east-1")
    plan = choose_actions({}, [ROW_RESTART, ROW_KEY], ctx)
    assert plan["action_ids"] == ["a-restart", "a-key"] and plan["chosen_by"] == "policy"
    good = choose_actions({}, [ROW_RESTART, ROW_KEY], ctx, invoke=lambda s, u: '```json\n{"action_ids": ["a-key"], "rationale": "r"}\n```')
    assert good["action_ids"] == ["a-key"] and good["chosen_by"] == "agent"
    bad = choose_actions({}, [ROW_RESTART, ROW_KEY], ctx, invoke=lambda s, u: '{"action_ids": ["a-restart", "nuke-everything"]}')
    assert bad["action_ids"] == ["a-restart", "a-key"] and "rejected" in bad["rationale"]
    broken = choose_actions({}, [ROW_RESTART], ctx, invoke=lambda s, u: (_ for _ in ()).throw(RuntimeError("down")))
    assert broken["action_ids"] == ["a-restart"] and "unavailable" in broken["rationale"]
    assert choose_actions({}, [ROW_OTHER], ctx)["action_ids"] == []


def test_autopilot_runs_both_actions_and_marks_the_incident(env):
    marked = env
    table = _Table([ROW_RESTART, ROW_KEY])
    session = _Session()
    notes = []
    [datetime.now(timezone.utc).timestamp()]
    out = respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: session, notify=lambda text, kind: notes.append(kind))
    decisions = out["action"]["decisions"]
    assert [d["action_type"] for d in decisions] == ["ecs_restart_task", "iam_deactivate_key"]
    assert all(d["decision"] == "run" for d in decisions) and all(d["risk"] <= 2 for d in decisions)
    assert session.ecs.stopped[0]["reason"] == "incident-abc" and session.iam.status == "Inactive"
    assert len(table.audits) == 2 and all(a["status"] == "verified" for a in table.audits.values())
    assert marked["action_verified_at"] and marked["action_started_at"] and marked["action_chosen_at"]
    assert len(marked["audit_ids"]) == 2 and out["action"]["pending"] == []
    assert notes == ["contained"]


def test_approve_mode_parks_the_action_then_runs_on_approval(env):
    marked = env
    table = _Table([dict(ROW_RESTART, mode="approve")])
    session = _Session()
    notes = []
    out = respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: session, notify=lambda text, kind: notes.append(kind))
    assert session.ecs.stopped == [] and out["action"]["pending"][0]["action_id"] == "a-restart"
    assert "action_started_at" not in marked and notes == ["pending"]

    done = approve_pending_action(table=table, pk=PK, incident_id="1-abc", action_id="a-restart", region="us-east-1", session_factory=lambda: session, approved_by="ilia")
    assert session.ecs.stopped and done["audit"]["approved_by"] == "ilia" and done["audit"]["status"] == "verified"
    assert marked["action"]["pending"] == [] and marked["action_verified_at"]
    with pytest.raises(ValueError):
        approve_pending_action(table=table, pk=PK, incident_id="1-abc", action_id="a-restart", region="us-east-1", session_factory=lambda: session, approved_by="ilia")


def test_risk_above_cap_waits_and_no_credentials_is_recorded(env):
    table = _Table([dict(ROW_RESTART, risk_max=1)])
    out = respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: None, calibration_cfg={"ECS_TASK_RESTART": 3})
    assert out["action"]["decisions"][0]["decision"] == "wait_for_approval"
    table = _Table([ROW_RESTART])
    out = respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: None)
    assert out["action"]["decisions"][0]["decision"] == "skipped" and "aws.roles.act" in out["action"]["decisions"][0]["reason"]


def test_nothing_applicable_is_recommend_only(env):
    notes = []
    out = respond_to_incident(table=_Table([ROW_OTHER]), pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: None, notify=lambda text, kind: notes.append(kind))
    assert out["action"]["plan"]["action_ids"] == [] and notes == ["none"]


def test_context_region_follows_the_target_arns():
    ctx = context_from_incident(INCIDENT, ALERTS, region="eu-central-1")
    assert ctx.region == "us-east-1", "the ARNs say us-east-1; the workspace default must not win"


def test_a_retry_never_runs_the_action_twice(env):
    marked = env
    table = _Table([ROW_RESTART])
    session = _Session()
    respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: session)
    assert len(session.ecs.stopped) == 1 and marked["action_started_at"]
    again = respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: session)
    assert again.get("skipped") == "already responded" and len(session.ecs.stopped) == 1
    respond_to_incident(table=table, pk=PK, incident_id="1-abc", region="us-east-1", session_factory=lambda: session, force=True)
    assert len(session.ecs.stopped) == 2
