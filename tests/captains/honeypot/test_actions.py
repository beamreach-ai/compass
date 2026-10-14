"""HP-27: the action catalog — snapshot, execute, verify, rollback, scope."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from captains.honeypot.actions import ACTION_CATALOG, catalog, get_action
from captains.honeypot.actions.base import ActionContext, ActionScope, ScopeError
from captains.honeypot.policy import gate, score_action

REGION = "us-east-1"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        yield boto3.Session(region_name=REGION)


class _FakeEcs:
    """moto's ECS cannot run Fargate tasks, so snapshot/execute are exercised
    against a recording fake with the real response shapes."""

    def __init__(self):
        self.cluster = "arn:aws:ecs:us-east-1:1:cluster/web"
        self.service = "arn:aws:ecs:us-east-1:1:service/web/web"
        self.task = "arn:aws:ecs:us-east-1:1:task/web/abc123"
        self.td = "arn:aws:ecs:us-east-1:1:task-definition/web:3"
        self.stopped = []

    def describe_services(self, **kw):
        return {"services": [{"serviceName": "web", "desiredCount": 1, "runningCount": 1, "taskDefinition": self.td,
                              "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}},
                              "deployments": [{"status": "PRIMARY", "taskDefinition": self.td, "runningCount": 1}], "events": []}]}

    def describe_tasks(self, **kw):
        return {"tasks": [{"taskArn": a, "lastStatus": "RUNNING", "taskDefinitionArn": self.td,
                           "containers": [{"name": "web", "image": "img", "imageDigest": "sha256:1"}],
                           "attachments": [{"details": [{"name": "privateIPv4Address", "value": "10.0.0.5"}]}],
                           "stoppedReason": next((s["reason"] for s in self.stopped if s["task"] == a), None)} for a in kw["tasks"]]}

    def describe_task_definition(self, **kw):
        return {"taskDefinition": {"containerDefinitions": [{"name": "web", "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/web", "awslogs-stream-prefix": "web"}}}]}}

    def list_tasks(self, **kw):
        return {"taskArns": [self.task]}

    def stop_task(self, **kw):
        self.stopped.append(kw)
        return {"ResponseMetadata": {"RequestId": "req-1"}}


class _Session:
    def __init__(self, client):
        self._client = client

    def client(self, *_a, **_k):
        return self._client


def test_catalog_lists_both_actions():
    types = {c["action_type"] for c in catalog()}
    assert types == {"ecs_restart_task", "iam_deactivate_key"} == set(ACTION_CATALOG)
    with pytest.raises(KeyError):
        get_action("ecs_delete_cluster")


def test_ecs_restart_snapshot_execute_and_stop_reason():
    ecs = _FakeEcs()
    session = _Session(ecs)
    action = get_action("ecs_restart_task")
    ctx = ActionContext(region=REGION, incident_id="inc-abc123def456", cluster_arn=ecs.cluster, service_arn=ecs.service, task_arn=ecs.task, environment="replay")

    snap = action.snapshot(session, ctx)
    assert snap["service"]["name"] == "web" and snap["service"]["desired_count"] == 1
    assert snap["task"]["arn"] == ecs.task and snap["task"]["containers"][0]["image_digest"] == "sha256:1"
    assert snap["log_streams"] == [{"container": "web", "log_group": "/ecs/web", "log_stream": "web/web/abc123"}]
    assert snap["running_tasks"] == [ecs.task]

    executed = action.execute(session, ctx, snap)
    assert executed["reason"] == "incident-abc123def456"
    assert ecs.stopped == [{"cluster": ecs.cluster, "task": ecs.task, "reason": "incident-abc123def456"}]
    assert executed["request_id"] == "req-1"
    assert "honeypot" not in executed["reason"] and "compass" not in executed["reason"]


class _ScriptedEcs:
    """What ECS looks like during a restart: the old task first, then a new
    RUNNING task, then a steady-state event newer than the stop."""

    def __init__(self, old, new, service_arn, stop_at):
        self.old, self.new, self.service_arn = old, new, service_arn
        self.calls = 0
        self.stop_at = stop_at

    def list_tasks(self, **kw):
        self.calls += 1
        return {"taskArns": [self.old] if self.calls < 2 else [self.new]}

    def describe_tasks(self, **kw):
        return {"tasks": [{"taskArn": a, "lastStatus": "RUNNING"} for a in kw["tasks"]]}

    def describe_services(self, **kw):
        created = self.stop_at + timedelta(seconds=40) if self.calls >= 3 else self.stop_at - timedelta(seconds=60)
        return {"services": [{
            "serviceName": "web", "runningCount": 1, "desiredCount": 1,
            "events": [{"message": "(service web) has reached a steady state.", "createdAt": created}],
        }]}


def test_ecs_restart_verify_waits_for_new_task_and_steady_state():
    action = get_action("ecs_restart_task")
    stop_at = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    ecs = _ScriptedEcs("arn:aws:ecs:us-east-1:1:task/web/old", "arn:aws:ecs:us-east-1:1:task/web/new", "arn:aws:ecs:us-east-1:1:service/web/web", stop_at)
    ctx = ActionContext(region=REGION, incident_id="inc-1", cluster_arn="arn:aws:ecs:us-east-1:1:cluster/web", service_arn=ecs.service_arn, task_arn=ecs.old)
    t = [stop_at.timestamp()]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s

    result = action.verify(_Session(ecs), ctx, {"cluster_arn": ctx.cluster_arn, "service": {"name": "web"}}, {"executed_at": stop_at.isoformat()}, sleep=sleep, clock=clock)
    assert result["ok"] is True
    assert result["new_task"].endswith("/new")
    assert result["steady_after_s"] >= result["new_task_running_after_s"]


def test_ecs_restart_verify_times_out():
    action = get_action("ecs_restart_task")
    stop_at = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)

    class Stuck(_ScriptedEcs):
        def list_tasks(self, **kw):
            return {"taskArns": [self.old]}

    ecs = Stuck("arn:aws:ecs:us-east-1:1:task/web/old", "n/a", "arn:aws:ecs:us-east-1:1:service/web/web", stop_at)
    ctx = ActionContext(region=REGION, incident_id="inc-1", cluster_arn="arn:aws:ecs:us-east-1:1:cluster/web", service_arn=ecs.service_arn, task_arn=ecs.old)
    t = [0.0]
    result = action.verify(_Session(ecs), ctx, {"cluster_arn": ctx.cluster_arn, "service": {"name": "web"}}, {"executed_at": None}, timeout_seconds=10, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert result["ok"] is False and "timeout" in result["reason"]


def test_ecs_restart_scope_and_iam_statements():
    action = get_action("ecs_restart_task")
    scope = ActionScope(service_arns=["arn:aws:ecs:us-east-1:1:service/web/web"])
    ok = ActionContext(region=REGION, incident_id="i", service_arn=scope.service_arns[0], task_arn="arn:aws:ecs:us-east-1:1:task/web/abc")
    action.check_scope(scope, ok)
    with pytest.raises(ScopeError):
        action.check_scope(scope, ActionContext(region=REGION, incident_id="i", service_arn="arn:aws:ecs:us-east-1:1:service/other/api", task_arn="arn:aws:ecs:us-east-1:1:task/other/abc"))
    with pytest.raises(ScopeError):
        action.check_scope(scope, ActionContext(region=REGION, incident_id="i", service_arn=scope.service_arns[0], task_arn="arn:aws:ecs:us-east-1:1:task/other/abc"))

    stmts = action.iam_statements(scope, account_id="1", region=REGION)
    stop = next(s for s in stmts if "ecs:StopTask" in s["Action"])
    assert stop["Resource"] == ["arn:aws:ecs:us-east-1:1:task/web/*"]
    assert stop["Condition"]["ArnEquals"]["ecs:cluster"] == ["arn:aws:ecs:us-east-1:1:cluster/web"]
    assert not any("ecs:UpdateService" in s["Action"] for s in stmts)
    assert action.iam_statements(ActionScope(), account_id="1", region=REGION) == []


def test_iam_deactivate_key_round_trip(aws):
    iam = aws.client("iam")
    iam.create_user(UserName="svc-reporting", Path="/service/")
    key = iam.create_access_key(UserName="svc-reporting")["AccessKey"]["AccessKeyId"]
    action = get_action("iam_deactivate_key")
    ctx = ActionContext(region=REGION, incident_id="inc-1", user_name="svc-reporting", user_arn="arn:aws:iam::123456789012:user/service/svc-reporting", access_key_id=key)

    snap = action.snapshot(aws, ctx)
    assert snap["keys"] == [{"access_key_id": key, "status": "Active"}]
    action.execute(aws, ctx, snap)
    assert action.verify(aws, ctx, snap, {}, clock=lambda: 0, sleep=lambda s: None)["ok"] is True
    assert iam.list_access_keys(UserName="svc-reporting")["AccessKeyMetadata"][0]["Status"] == "Inactive"
    assert action.rollback(aws, ctx, snap)["status"] == "Active"
    assert iam.list_access_keys(UserName="svc-reporting")["AccessKeyMetadata"][0]["Status"] == "Active"


def test_iam_deactivate_scope_by_path_or_arn():
    action = get_action("iam_deactivate_key")
    by_path = ActionScope(user_path_prefixes=["/service/"])
    action.check_scope(by_path, ActionContext(region=REGION, incident_id="i", user_arn="arn:aws:iam::1:user/service/x"))
    with pytest.raises(ScopeError):
        action.check_scope(by_path, ActionContext(region=REGION, incident_id="i", user_arn="arn:aws:iam::1:user/admin"))
    stmts = action.iam_statements(by_path, account_id="1", region=REGION)
    assert stmts[0]["Resource"] == ["arn:aws:iam::1:user/service/*"]
    assert set(stmts[0]["Action"]) == {"iam:UpdateAccessKey", "iam:ListAccessKeys"}


def test_scores_are_within_autopilot_reach_and_gate_ignores_urgency():
    ecs = get_action("ecs_restart_task")
    ctx = ActionContext(region=REGION, incident_id="i", task_arn="arn:aws:ecs:us-east-1:1:task/web/abc", environment="replay")
    scored = score_action(ecs, ctx, {"service": {"desired_count": 1, "running_count": 1}})
    assert 1 <= scored["risk"] <= 2, scored
    iam = get_action("iam_deactivate_key")
    assert score_action(iam, ActionContext(region=REGION, incident_id="i", access_key_id="AKIA"), {})["risk"] <= 2

    assert gate("autopilot", 2, 2)["decision"] == "run"
    assert gate("autopilot", 3, 2)["decision"] == "wait_for_approval"
    assert gate("approve", 1, 5)["decision"] == "wait_for_approval"
    assert gate("off", 1, 5)["decision"] == "recommend_only"
    assert gate("bogus", 1, 5)["decision"] == "recommend_only"
