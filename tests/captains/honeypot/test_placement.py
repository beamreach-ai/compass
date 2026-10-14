"""HP-04: one placement per honeytoken, embedded on the token item."""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from compass_api.routes import honeypots
from compass_core.keys import honeytoken_sort_key, workspace_pk

PK = workspace_pk("ws-a")
PLACEMENT_KEYS = {
    "type", "resource_arn", "service_arn", "cluster_arn", "container", "env_vars",
    "tf_address", "repo", "pr_url", "planted_at", "verified_at", "detail", "notes",
}
ECS = {
    "type": "ecs_task_env",
    "resource_arn": "arn:aws:ecs:us-east-1:1:task-definition/web:3",
    "service_arn": "arn:aws:ecs:us-east-1:1:service/c/web",
    "cluster_arn": "arn:aws:ecs:us-east-1:1:cluster/c",
    "container": "web",
    "env_vars": ["DB_HOST", "DB_USER"],
}


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
        table.put_item(Item={
            "pk": PK, "sk": honeytoken_sort_key("t1"), "honeytoken_id": "t1",
            "honeytoken_class": "aws_credential", "name": "svc", "status": "armed", "created_at": 1,
            "material": {"secret": "x"},
        })
        monkeypatch.setattr(honeypots, "_resolve_table", lambda request: table)
        monkeypatch.setattr(honeypots, "_ws_pk", lambda request: PK)
        monkeypatch.setattr(honeypots, "get_actor_ids", lambda request: {"workspace_id": "ws-a"})
        app = FastAPI()
        app.include_router(honeypots.router)
        yield TestClient(app, follow_redirects=False), table


def _stored(table):
    return table.get_item(Key={"pk": PK, "sk": honeytoken_sort_key("t1")})["Item"]


def test_put_stores_exactly_the_model_keys(env):
    client, table = env
    resp = client.put("/api/honeypots/tokens/t1/placement", json=ECS)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["placement"]) == PLACEMENT_KEYS
    assert body["placement"]["container"] == "web"
    assert body["placement"]["env_vars"] == ["DB_HOST", "DB_USER"]
    assert body["placement"]["pr_url"] is None
    assert set(_stored(table)["placement"]) == PLACEMENT_KEYS
    assert "material" not in body


def test_put_replaces_never_appends(env):
    client, table = env
    client.put("/api/honeypots/tokens/t1/placement", json=ECS)
    second = client.put("/api/honeypots/tokens/t1/placement", json={"type": "secrets_manager", "resource_arn": "arn:secret"})
    placement = second.json()["placement"]
    assert placement["type"] == "secrets_manager"
    assert placement["container"] is None and placement["env_vars"] is None and placement["service_arn"] is None
    assert not isinstance(_stored(table)["placement"], list)
    assert not [i for i in table.scan()["Items"] if "PLACEMENT" in i["sk"]]


def test_delete_clears(env):
    client, table = env
    client.put("/api/honeypots/tokens/t1/placement", json=ECS)
    resp = client.delete("/api/honeypots/tokens/t1/placement")
    assert resp.status_code == 200 and resp.json()["placement"] is None
    assert _stored(table)["placement"] is None
    assert client.get("/api/honeypots/tokens/t1").json()["placement"] is None


def test_manual_needs_only_detail_and_notes(env):
    client, _ = env
    resp = client.put("/api/honeypots/tokens/t1/placement", json={"type": "manual", "detail": "Confluence: runbook", "notes": " n "})
    placement = resp.json()["placement"]
    assert placement["type"] == "manual" and placement["detail"] == "Confluence: runbook" and placement["notes"] == "n"
    assert placement["resource_arn"] is None


def test_invalid_type_and_missing_token(env):
    client, _ = env
    assert client.put("/api/honeypots/tokens/t1/placement", json={"type": "wiki"}).status_code == 422
    assert client.put("/api/honeypots/tokens/t1/placement", json={}).status_code == 422
    assert client.put("/api/honeypots/tokens/nope/placement", json=ECS).status_code == 404
    assert client.delete("/api/honeypots/tokens/nope/placement").status_code == 404


def test_serialization_includes_placement_not_count(env):
    client, _ = env
    listed = client.get("/api/honeypots/tokens").json()[0]
    assert listed["placement"] is None and "placement_count" not in listed
    client.put("/api/honeypots/tokens/t1/placement", json=ECS)
    listed = client.get("/api/honeypots/tokens").json()[0]
    assert listed["placement"]["type"] == "ecs_task_env" and "placement_count" not in listed
    assert client.get("/api/honeypots/tokens/t1").json()["placement"]["container"] == "web"


def test_old_placements_routes_are_gone(env):
    client, _ = env
    assert client.get("/api/honeypots/tokens/t1/placements").status_code in (404, 405)
    assert client.post("/api/honeypots/tokens/t1/placements", json={"location_type": "wiki"}).status_code in (404, 405)
    assert client.delete("/api/honeypots/tokens/t1/placements/p1").status_code in (404, 405)


def test_legacy_redirects_still_work(env):
    client, _ = env
    resp = client.get("/api/honeypots/t1")
    assert resp.status_code == 308 and resp.headers["location"] == "/api/honeypots/tokens/t1"
    resp = client.get("/api/honeypots/t1/lure")
    assert resp.status_code == 308 and resp.headers["location"] == "/api/honeypots/tokens/t1/material"
    assert client.get("/api/honeypots").status_code == 308


def test_material_endpoint_shape_unchanged(env):
    client, _ = env
    body = client.get("/api/honeypots/tokens/t1/material").json()
    assert body["material"] == {"secret": "x"}


def test_create_accepts_placement(env, monkeypatch):
    client, table = env
    monkeypatch.setattr(honeypots._queue_client, "enqueue", lambda **kw: {"job_id": "j"})
    resp = client.post("/api/honeypots/tokens", json={"honeytoken_class": "aws_credential", "name": "svc-two", "placement": ECS})
    assert resp.status_code == 200, resp.text
    assert resp.json()["placement"]["service_arn"] == ECS["service_arn"]
    no_placement = client.post("/api/honeypots/tokens", json={"honeytoken_class": "aws_credential", "name": "svc-three"})
    assert no_placement.json()["placement"] is None


def test_placement_summary_attribution():
    assert honeypots._placement_summary(None) == ""
    text = honeypots._placement_summary(ECS)
    assert "Planted in ecs task env" in text and ECS["resource_arn"] in text
    assert f"likely breach source: {ECS['service_arn']}" in text
    manual = honeypots._placement_summary({"type": "manual", "detail": "Confluence: runbook"})
    assert "likely breach source: Confluence: runbook" in manual


def test_suggestion_placement_prefill():
    assert honeypots._suggestion_placement("s3_bucket", "b", {}) is None
    out = honeypots._suggestion_placement("ecs_task_definition", "arn:td", {})
    assert out == {"type": "ecs_task_env", "resource_arn": "arn:td"}
    svc = honeypots._suggestion_placement("ecs_service", "arn:svc", {"task_definition_arn": "arn:td"})
    assert svc["service_arn"] == "arn:svc" and svc["resource_arn"] == "arn:td"


def test_create_without_a_name_picks_a_cover_name(env):
    client, _ = env
    resp = client.post("/api/honeypots/tokens", json={"honeytoken_class": "aws_credential", "name": ""})
    assert resp.status_code == 200, resp.text
    picked = resp.json()["name"].lower()
    assert picked and ("cana" + "ry") not in picked and "honey" not in picked, "cover names carry no product word"
