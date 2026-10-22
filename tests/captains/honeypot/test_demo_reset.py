"""The honeypot's part of the demo reset (docs/demo.md): open incidents closed as "demo reset",
verified key deactivations rolled back, the pre-armed decoys armed again, the replay service
waited for; a second run has nothing to do. On moto."""

from __future__ import annotations

from typing import Any, Dict

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import arm as arming
from captains.honeypot import demo_reset, incidents
from compass_core import config as config_module
from compass_core import sessions
from compass_core.keys import honeypot_audit_sort_key, honeypot_incident_sort_key, honeytoken_sort_key, workspace_pk

WS = "ws-demo"
PK = workspace_pk(WS)
CONFIG: Dict[str, Any] = {"workspace_id": WS, "org_id": "acme", "dynamodb_config": {"workspace_id": WS, "items": "items", "map": "map"},
                          "aws": {"account_id": "123456789012", "region": "us-east-1",
                                  "roles": {"read": "arn:aws:iam::123456789012:role/r", "act": "arn:aws:iam::123456789012:role/a"}},
                          "demo": {"trippable": ["tok-1"]}}
SERVICE = "arn:aws:ecs:us-east-1:123456789012:service/replay/web"


class FakeEcs:
    def __init__(self, counts):
        self.counts = list(counts)

    def describe_services(self, cluster, services):
        running, desired = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        return {"services": [{"serviceArn": services[0], "runningCount": running, "desiredCount": desired}]}


class FakeSession:
    def __init__(self, ecs):
        self.ecs = ecs
        self.region_name = "us-east-1"

    def client(self, name, **kw):
        return self.ecs if name == "ecs" else boto3.Session(region_name="us-east-1").client(name)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setattr(config_module, "_loader", lambda ws: dict(CONFIG))
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_user(UserName="svc-backup", Path="/service/")
        key = iam.create_access_key(UserName="svc-backup")["AccessKey"]["AccessKeyId"]
        iam.update_access_key(UserName="svc-backup", AccessKeyId=key, Status="Inactive")
        table.put_item(Item={"pk": PK, "sk": honeytoken_sort_key("tok-1"), "honeytoken_id": "tok-1", "honeytoken_class": "aws_credential", "name": "svc-backup",
                             "status": "armed", "region": "us-east-1", "material": {"access_key_id": key, "secret_access_key": "s"},
                             "placement": {"service_arn": SERVICE}, "trigger_count": 0, "created_at": 1, "updated_at": 1})
        table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-1"), "incident_id": "inc-1", "status": "open", "honeytoken_ids": ["tok-1"],
                             "audit_ids": ["aud-1"], "action_verified_at": "2026-10-21T10:01:00Z"})
        table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-0"), "incident_id": "inc-0", "status": "closed", "closed_at": "2026-10-20T10:00:00Z"})
        table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key("aud-1"), "audit_id": "aud-1", "incident_id": "inc-1", "action_type": "iam_deactivate_key",
                             "status": "verified", "target": {"region": "us-east-1", "user_arn": "arn:aws:iam::123456789012:user/service/svc-backup", "access_key_id": key},
                             "rollback": {"supported": True, "done_at": None}})
        ecs = FakeEcs([(0, 1), (1, 1)])
        monkeypatch.setattr(sessions, "target", lambda *a, **k: FakeSession(ecs))
        monkeypatch.setattr(arming, "verify_honeytoken", lambda session, token, placement=None, *, config=None: {"ok": True, "checks": [], "access_key_id": key})
        yield table, iam, key


def test_the_reset_closes_rolls_back_rearms_and_waits_then_has_nothing_to_do(env, monkeypatch):
    table, iam, key = env
    slept = []
    monkeypatch.setattr(demo_reset, "STEADY_STATE_TIMEOUT_S", 60.0)
    first = demo_reset.demo_reset({"workspace_id": WS})
    assert first["incidents_closed"] == 1 and first["rolled_back"] == 1 and first["rearmed"] == ["tok-1"] and first["failed"] == []
    assert first["steady_state"] == {SERVICE: {"steady": True, "running": 1, "desired": 1}}
    closed = incidents.get_incident(table, PK, "inc-1")
    assert closed["status"] == "closed" and closed["closed_at"] and closed["resolution"] == "demo reset"
    assert iam.list_access_keys(UserName="svc-backup")["AccessKeyMetadata"][0]["Status"] == "Active"
    audit = table.get_item(Key={"pk": PK, "sk": honeypot_audit_sort_key("aud-1")})["Item"]
    assert audit["rollback"]["done_at"] and audit["rollback"]["requested_by"] == "demo-rearm"
    assert table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key("tok-1")})["Item"]["status"] == "armed"

    second = demo_reset.demo_reset({"workspace_id": WS})
    assert second["incidents_closed"] == 0 and second["rolled_back"] == 0 and second["rearmed"] == ["tok-1"] and second["failed"] == []
    assert not slept


def test_outside_demo_mode_the_reset_refuses(env, monkeypatch):
    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(ValueError, match="runs only in demo mode"):
        demo_reset.demo_reset({"workspace_id": WS})


def test_without_a_service_in_a_placement_there_is_nothing_to_wait_for(env):
    table, _, _ = env
    table.update_item(Key={"pk": PK, "sk": honeytoken_sort_key("tok-1")}, UpdateExpression="REMOVE placement")
    assert demo_reset.wait_for_steady_state(table, PK, ["tok-1"], CONFIG) is None
