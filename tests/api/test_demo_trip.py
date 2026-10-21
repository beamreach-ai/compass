"""Trip it (docs/demo.md): a pre-armed decoy fired for real by a visitor, paced. On moto: a
decoy that is not trippable or not armed is refused; the first trip claims the cooldown and
queues the job; the next visitor within the cooldown is sent to the incident; a visitor's
hourly limit; test-fire hidden in demo mode; the trip job's STS call, its alert and its
incident; the re-arm once the response verified (the key rolled back, the token armed)."""

from __future__ import annotations

from typing import Any, Dict, List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from captains.honeypot import arm as arming
from captains.honeypot import commands as honeypot_commands
from captains.honeypot import demo_rearm
from captains.honeypot.ingestion import record_alert
from compass_api.routes import honeypots
from compass_core import config as config_module
from compass_core import demo, sessions
from compass_core.keys import honeypot_audit_sort_key, honeypot_incident_sort_key, honeytoken_sort_key, workspace_pk

WS = "ws-demo"
PK = workspace_pk(WS)
ARMED, PLANT = "tok-armed", "tok-plant"
CONFIG: Dict[str, Any] = {
    "workspace_id": WS, "org_id": "acme",
    "dynamodb_config": {"workspace_id": WS, "items": "items", "map": "map"},
    "aws": {"account_id": "123456789012", "region": "us-east-1", "external_id": "ext",
            "roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-k3f9a2",
                      "act": "arn:aws:iam::123456789012:role/service/svc-ops-responder-k3f9a2"}},
    "demo": {"trippable": [ARMED], "cooldown_seconds": 300, "trips_per_visitor_per_hour": 2},
}


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: List[Dict[str, Any]] = []

    def enqueue(self, **kw: Any) -> Dict[str, Any]:
        self.jobs.append(kw)
        return {"job_id": f"job-{len(self.jobs)}"}


def _token(token_id: str, status: str, *, with_secret: bool = True) -> Dict[str, Any]:
    material = {"access_key_id": "AKIA" + token_id.upper().replace("-", "")[:16].ljust(16, "X"), "region": "us-east-1"}
    if with_secret:
        material["secret_access_key"] = "secret-" + token_id
    return {"pk": PK, "sk": honeytoken_sort_key(token_id), "honeytoken_id": token_id, "honeytoken_class": "aws_credential", "name": token_id,
            "status": status, "region": "us-east-1", "material": material, "trigger_count": 0, "created_at": 1, "updated_at": 1,
            "placement": {"service_arn": "arn:aws:ecs:us-east-1:123456789012:service/replay/web"}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("APP_MODE", "demo")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item=_token(ARMED, "armed"))
        table.put_item(Item=_token(PLANT, "pending_merge"))
        queue = FakeQueue()
        actor: Dict[str, Any] = {"workspace_id": WS, "org_id": "acme", "user_id": "visitor-1"}
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
        monkeypatch.setattr(honeypots, "get_actor_ids", lambda request: dict(actor))
        monkeypatch.setattr(honeypots, "get_workspace_config", lambda request: dict(CONFIG))
        monkeypatch.setattr(honeypots, "_queue_client", queue)
        monkeypatch.setattr(config_module, "_loader", lambda ws: dict(CONFIG))
        app = FastAPI()
        app.include_router(honeypots.router)
        yield TestClient(app), table, queue, actor


def test_only_an_armed_trippable_decoy_can_be_tripped_and_test_fire_is_hidden(env):
    client, table, queue, actor = env
    assert client.post(f"/api/honeypots/tokens/{PLANT}/trip").status_code == 409, "a plant that was never applied"
    assert client.post("/api/honeypots/tokens/nope/trip").status_code == 404
    table.update_item(Key={"pk": PK, "sk": honeytoken_sort_key(ARMED)}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "pending_merge"})
    refused = client.post(f"/api/honeypots/tokens/{ARMED}/trip")
    assert refused.status_code == 409 and "re-arms" in refused.json()["detail"]
    assert client.post(f"/api/honeypots/tokens/{ARMED}/test-fire").status_code == 404, "test-fire is not part of the demo's story"
    assert queue.jobs == []


def test_the_first_trip_claims_the_cooldown_and_the_next_visitor_is_sent_to_the_incident(env):
    client, table, queue, actor = env
    first = client.post(f"/api/honeypots/tokens/{ARMED}/trip")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["state"] == "tripped" and body["by"] == "you" and body["job_id"] == "job-1" and 290 <= body["seconds_left"] <= 300
    assert queue.jobs[0]["captain"] == "honeypot" and queue.jobs[0]["command"] == "trip"
    assert queue.jobs[0]["payload"] == {"honeytoken_id": ARMED, "workspace_id": WS, "org_id": "acme", "user_id": "visitor-1"}

    # the job reports the incident on the row; the next visitor sees it, not a second fire
    demo.record_trip(table, PK, ARMED, incident_id="inc-1", fired_at="2026-10-21T10:00:00Z")
    actor["user_id"] = "visitor-2"
    second = client.post(f"/api/honeypots/tokens/{ARMED}/trip").json()
    assert second["state"] == "cooldown" and second["incident_id"] == "inc-1" and second["by"] == "a visitor" and second["in_force"] is True
    assert len(queue.jobs) == 1

    # the cooldown over, the decoy can be tripped again; a visitor has two an hour
    table.update_item(Key={"pk": PK, "sk": demo.demo_cooldown_sort_key(ARMED)}, UpdateExpression="SET #u = :u", ExpressionAttributeNames={"#u": "until"}, ExpressionAttributeValues={":u": 1})
    assert client.post(f"/api/honeypots/tokens/{ARMED}/trip").json()["state"] == "tripped"
    table.update_item(Key={"pk": PK, "sk": demo.demo_cooldown_sort_key(ARMED)}, UpdateExpression="SET #u = :u", ExpressionAttributeNames={"#u": "until"}, ExpressionAttributeValues={":u": 1})
    actor["user_id"] = "visitor-1"
    assert client.post(f"/api/honeypots/tokens/{ARMED}/trip").json()["state"] == "tripped"
    table.update_item(Key={"pk": PK, "sk": demo.demo_cooldown_sort_key(ARMED)}, UpdateExpression="SET #u = :u", ExpressionAttributeNames={"#u": "until"}, ExpressionAttributeValues={":u": 1})
    limited = client.post(f"/api/honeypots/tokens/{ARMED}/trip")
    assert limited.status_code == 429 and "2 trips an hour" in limited.json()["detail"]


def test_the_trip_job_fires_the_key_waits_for_the_alert_and_records_the_incident(env, monkeypatch):
    client, table, queue, actor = env
    fired: List[Dict[str, Any]] = []

    def fake_fire(key_id, secret, region, *, user_agent=arming.TEST_FIRE_USER_AGENT, client_factory=None):
        fired.append({"key_id": key_id, "secret": secret, "region": region, "user_agent": user_agent})
        # what CloudTrail → EventBridge → the queue → the consumer would do, a moment later
        record_alert(table, PK, {"honeytoken_id": ARMED, "token_id": ARMED, "event_name": "GetCallerIdentity", "access_key_id": key_id,
                                 "source_ip": "203.0.113.9", "user_agent": f"aws-sdk-python {user_agent}", "principal_arn": "arn:aws:iam::123456789012:user/service/svc-backup",
                                 "workspace_id": WS}, workspace_id=WS, queue_client=FakeQueue())

    monkeypatch.setattr(arming, "fire_aws_credential", fake_fire)
    demo.claim_trip(table, PK, ARMED, user_id="visitor-1", cooldown_seconds=300, now_ms=demo.epoch_ms())
    with sessions.captain_context("honeypot", requester="visitor-1"):
        result = honeypot_commands.trip({"workspace_id": WS, "honeytoken_id": ARMED, "timeout": 5})
    assert fired == [{"key_id": _token(ARMED, "armed")["material"]["access_key_id"], "secret": "secret-" + ARMED, "region": "us-east-1", "user_agent": arming.TRIP_USER_AGENT}]
    assert result["status"] == "delivered" and result["alert_id"] and result["incident_id"], result
    row = demo.cooldown_row(table, PK, ARMED)
    assert row["incident_id"] == result["incident_id"] and row["alert_id"] == result["alert_id"] and row["fired_at"] == result["fired_at"]
    incident = table.get_item(Key={"pk": PK, "sk": honeypot_incident_sort_key(result["incident_id"])})["Item"]
    assert incident["status"] == "open" and "is_test" not in incident, "a trip is a real incident, not a test"

    # outside demo mode the command refuses
    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(ValueError, match="runs only in demo mode"):
        honeypot_commands.trip({"workspace_id": WS, "honeytoken_id": ARMED})


def test_the_rearm_waits_for_the_verified_response_then_rolls_the_key_back_and_arms(env, monkeypatch):
    client, table, queue, actor = env
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_user(UserName="svc-backup", Path="/service/")
    key = iam.create_access_key(UserName="svc-backup")["AccessKey"]["AccessKeyId"]
    iam.update_access_key(UserName="svc-backup", AccessKeyId=key, Status="Inactive")
    table.put_item(Item={"pk": PK, "sk": honeypot_incident_sort_key("inc-1"), "incident_id": "inc-1", "status": "open", "honeytoken_ids": [ARMED], "audit_ids": ["aud-1"]})
    table.put_item(Item={"pk": PK, "sk": honeypot_audit_sort_key("aud-1"), "audit_id": "aud-1", "incident_id": "inc-1", "action_type": "iam_deactivate_key",
                         "status": "verified", "target": {"region": "us-east-1", "user_arn": "arn:aws:iam::123456789012:user/service/svc-backup", "access_key_id": key},
                         "rollback": {"supported": True, "done_at": None}})
    table.update_item(Key={"pk": PK, "sk": honeytoken_sort_key(ARMED)}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "tripped"})
    demo.claim_trip(table, PK, ARMED, user_id="visitor-1", cooldown_seconds=300, now_ms=demo.epoch_ms())
    demo.record_trip(table, PK, ARMED, incident_id="inc-1")
    monkeypatch.setattr(sessions, "target", lambda *a, **k: boto3.Session(region_name="us-east-1"))
    monkeypatch.setattr(arming, "verify_honeytoken", lambda session, token, placement=None, *, config=None: {"ok": True, "checks": [], "access_key_id": key})

    waiting = demo_rearm.rearm_once(CONFIG, workspace_id=WS, table=table)
    assert waiting == {"rearmed": [], "waiting": [ARMED], "failed": []}, "nothing before the response verified"
    assert iam.list_access_keys(UserName="svc-backup")["AccessKeyMetadata"][0]["Status"] == "Inactive"

    table.update_item(Key={"pk": PK, "sk": honeypot_incident_sort_key("inc-1")}, UpdateExpression="SET action_verified_at = :t", ExpressionAttributeValues={":t": "2026-10-21T10:01:00Z"})
    done = demo_rearm.rearm_once(CONFIG, workspace_id=WS, table=table)
    assert done["rearmed"] == [ARMED] and done["failed"] == [], done
    assert iam.list_access_keys(UserName="svc-backup")["AccessKeyMetadata"][0]["Status"] == "Active"
    audit = table.get_item(Key={"pk": PK, "sk": honeypot_audit_sort_key("aud-1")})["Item"]
    assert audit["rollback"]["done_at"] and audit["rollback"]["requested_by"] == "demo-rearm"
    token = table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key(ARMED)})["Item"]
    assert token["status"] == "armed" and token["material"]["access_key_id"] == key
    row = demo.cooldown_row(table, PK, ARMED)
    assert row["rearmed_at"] and row["incident_id"] == "inc-1", "the incident stays for the next visitor to read"
    assert table.get_item(Key={"pk": PK, "sk": honeypot_incident_sort_key("inc-1")})["Item"]["status"] == "open"
    # a second tick has nothing to do
    assert demo_rearm.rearm_once(CONFIG, workspace_id=WS, table=table) == {"rearmed": [], "waiting": [], "failed": []}

    # the trip route reports the trip, in force, with the incident, to anyone
    actor["user_id"] = "visitor-2"
    seen = client.post(f"/api/honeypots/tokens/{ARMED}/trip").json()
    assert seen["state"] == "cooldown" and seen["incident_id"] == "inc-1" and seen["rearmed_at"]
