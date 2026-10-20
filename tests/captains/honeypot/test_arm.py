"""HP-14: /arm verifies the target account read-only; test-fire does a real round trip."""

from __future__ import annotations

import json

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.honeypot import arm
from captains.honeypot.terraform_generator import stealth_tag
from compass_api.routes import honeypots
from compass_core.keys import honeypot_alert_sort_key, honeytoken_sort_key, workspace_pk

PK = workspace_pk("ws-a")
TOKEN = {"honeytoken_id": "t1", "honeytoken_class": "aws_credential", "name": "svc-backup-sync"}
COVER = "svc-backup-sync"


def _plant(session, *, tag=True, rule_state="ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS", keys=1, trail=True):
    iam = session.client("iam")
    iam.create_user(UserName=COVER, Tags=[{"Key": "cost-center", "Value": stealth_tag("t1")[1] if tag else "deadbeef"}])
    for _ in range(keys):
        iam.create_access_key(UserName=COVER)
    ev = session.client("events")
    ev.put_rule(Name=f"{COVER}-activity", EventPattern=json.dumps({"source": ["aws.iam"]}), State="ENABLED")
    if trail:
        s3 = session.client("s3")
        s3.create_bucket(Bucket="trail-bucket")
        ct = session.client("cloudtrail")
        ct.create_trail(Name="main", S3BucketName="trail-bucket")
        ct.start_logging(Name="main")


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


def _checks(res):
    return {c["name"]: c for c in res["checks"]}


def test_verify_aws_credential_missing_trail_refuses(aws):
    _plant(aws, trail=False)
    res = arm.verify_honeytoken(aws, TOKEN, None)
    assert not res["ok"]
    assert arm.NO_TRAIL_DETAIL in _checks(res)["cloudtrail_logging"]["detail"]
    assert aws.client("cloudtrail").describe_trails()["trailList"] == []  # never created one


def test_verify_aws_credential_wrong_tag_and_two_keys(aws):
    _plant(aws, tag=False, keys=2)
    c = _checks(arm.verify_honeytoken(aws, TOKEN, None))
    assert not c["stealth_tag"]["ok"]
    assert not c["access_key"]["ok"]


def test_verify_aws_credential_ok_returns_key_id(aws):
    _plant(aws)
    res = arm.verify_honeytoken(aws, TOKEN, None)
    c = _checks(res)
    assert c["iam_user"]["ok"] and c["stealth_tag"]["ok"] and c["cloudtrail_logging"]["ok"]
    assert res["access_key_id"].startswith("AKIA")
    # moto stores plain ENABLED; the strict state and target role are real failures here
    assert not c["rule_state"]["ok"]


def test_verify_nothing_planted(aws):
    res = arm.verify_honeytoken(aws, TOKEN, None)
    assert not res["ok"] and "access_key_id" not in res


def test_verify_db_credential_listener_not_running(aws):
    token = {"honeytoken_id": "t2", "honeytoken_class": "db_credential", "name": "db-backup"}
    res = arm.verify_honeytoken(aws, token, None)
    assert not res["ok"]
    assert _checks(res)["dns_check"]["detail"] == "skipped (private zone)"


def test_verify_db_credential_running():
    class Ecs:
        def list_clusters(self): return {"clusterArns": ["arn:aws:ecs:us-east-1:1:cluster/c"]}
        def describe_services(self, cluster, services): return {"services": [{"status": "ACTIVE", "runningCount": 1}]}

    class Sess:
        def client(self, name):
            if name == "ecs":
                return Ecs()
            raise RuntimeError("no tagging")

    token = {"honeytoken_id": "t2", "honeytoken_class": "db_credential", "name": "db-backup"}
    assert arm.verify_honeytoken(Sess(), token, None)["ok"]


# ── routes ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(aws, monkeypatch):
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="items",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item={
        "pk": PK, "sk": honeytoken_sort_key("t1"), "honeytoken_id": "t1", "honeytoken_class": "aws_credential",
        "name": COVER, "status": "pending_merge", "created_at": 1,
        "material": {"access_key_id": None, "secret_access_key": None, "status": "pending_apply"},
    })
    monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
    monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
    monkeypatch.setattr(honeypots, "get_actor_ids", lambda request: {"workspace_id": "ws-a"})
    monkeypatch.setattr(honeypots, "get_workspace_config", lambda request: {})
    monkeypatch.setattr("compass_core.sessions.target", lambda *a, **k: aws)
    monkeypatch.setattr(honeypots._queue_client, "enqueue", lambda **k: None)
    app = FastAPI()
    app.include_router(honeypots.router)
    return TestClient(app), table


def test_arm_route_refuses_without_trail(api, aws):
    client, table = api
    _plant(aws, trail=False)
    r = client.post("/api/honeypots/tokens/t1/arm")
    assert r.status_code == 409
    assert not r.json()["detail"]["ok"]
    assert table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key("t1")})["Item"]["status"] == "pending_merge"


def test_arm_route_arms_and_fills_key(api, aws, monkeypatch):
    client, table = api
    _plant(aws)
    ok = {"ok": True, "checks": [], "access_key_id": "AKIAEXAMPLE"}
    monkeypatch.setattr(arm, "verify_honeytoken", lambda *a, **k: ok)
    r = client.post("/api/honeypots/tokens/t1/arm")
    assert r.status_code == 200 and r.json()["status"] == "armed"
    item = table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key("t1")})["Item"]
    assert item["material"]["access_key_id"] == "AKIAEXAMPLE" and item["material"]["status"] == "applied"


def test_test_fire_real_marks_alert_is_test(api, monkeypatch):
    client, table = api
    table.update_item(
        Key={"pk": PK, "sk": honeytoken_sort_key("t1")},
        UpdateExpression="SET material = :m",
        ExpressionAttributeValues={":m": {"access_key_id": "AKIA1", "secret_access_key": "s"}},
    )
    called = {}
    monkeypatch.setattr(arm, "fire_aws_credential", lambda *a, **k: called.setdefault("fired", True))

    def deliver(*a, **k):
        table.put_item(Item={
            "pk": PK, "sk": honeypot_alert_sort_key("t1", "a1"), "alert_id": "a1", "honeytoken_id": "t1",
            "processed_at": "9999-01-01T00:00:00+00:00", "user_agent": "aws-sdk compass-test-fire",
        })
        return arm.find_test_alert(table, PK, "t1", "0")

    monkeypatch.setattr(arm, "wait_for_test_alert", deliver)
    r = client.post("/api/honeypots/tokens/t1/test-fire")
    body = r.json()
    assert called["fired"] and body["mode"] == "real" and body["delivered"] and "round_trip_ms" in body
    stored = table.get_item(Key={"pk": PK, "sk": honeypot_alert_sort_key("t1", "a1")})["Item"]
    assert stored["is_test"] is True


def test_test_fire_real_times_out_as_not_delivered(api, monkeypatch):
    client, table = api
    table.update_item(
        Key={"pk": PK, "sk": honeytoken_sort_key("t1")},
        UpdateExpression="SET material = :m",
        ExpressionAttributeValues={":m": {"access_key_id": "AKIA1", "secret_access_key": "s"}},
    )
    monkeypatch.setattr(arm, "fire_aws_credential", lambda *a, **k: None)
    monkeypatch.setattr(honeypots, "_TEST_FIRE_TIMEOUT_S", 0.0)
    r = client.post("/api/honeypots/tokens/t1/test-fire")
    assert r.json()["status"] == "not_delivered" and r.json()["delivered"] is False


def test_test_fire_aws_without_secret_is_409(api):
    client, _ = api
    assert client.post("/api/honeypots/tokens/t1/test-fire").status_code == 409


def test_test_fire_db_credential_is_synthetic(api):
    client, table = api
    table.put_item(Item={"pk": PK, "sk": honeytoken_sort_key("t2"), "honeytoken_id": "t2",
                         "honeytoken_class": "db_credential", "name": "db-backup", "status": "armed"})
    body = client.post("/api/honeypots/tokens/t2/test-fire").json()
    assert body["mode"] == "synthetic" and body["is_test"] is True


def test_wait_for_test_alert_bounded():
    t = [0.0]
    class T:
        def query(self, **k): return {"Items": []}
    got = arm.wait_for_test_alert(T(), PK, "t1", "0", timeout_s=3, interval_s=1,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert got is None and t[0] == 3


def test_a_deactivated_key_is_not_armed(aws):
    """What a containment leaves behind: AWS rejects an inactive key before any
    rule sees the call, so it cannot trip (seen in the measured runs)."""
    _plant(aws)
    iam = aws.client("iam")
    key = iam.list_access_keys(UserName=COVER)["AccessKeyMetadata"][0]["AccessKeyId"]
    iam.update_access_key(UserName=COVER, AccessKeyId=key, Status="Inactive")
    c = _checks(arm.verify_honeytoken(aws, TOKEN, None))
    assert not c["access_key"]["ok"] and "roll back" in c["access_key"]["detail"]
