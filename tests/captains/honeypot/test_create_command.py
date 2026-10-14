"""`honeypot create` from the shell builds the same record the API does, then plants it."""

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import catalog
from captains.honeypot import commands as hp_commands
from compass_core import config as core_config
from compass_core import queue
from compass_core.keys import honeytoken_sort_key, workspace_pk

CONFIG = {"dynamodb_config": {"workspace_id": "ws-1", "audit": "items"}, "org_id": "org-1"}


@pytest.fixture
def local_workspace(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(core_config, "_loader", lambda ws: dict(CONFIG))
    previous = queue._factory
    yield
    queue.set_job_queue(previous)


def test_new_item_matches_the_api_shape():
    item = catalog.new_honeytoken_item(workspace_id="ws-1", honeytoken_class="aws_credential", region="us-east-1")
    assert item["pk"] == workspace_pk("ws-1") and item["sk"] == honeytoken_sort_key(item["honeytoken_id"])
    assert item["status"] == "pending_pr" and item["trigger_count"] == 0
    assert item["action_config"]["severity"] == "critical" and item["action_config"]["runbook"].startswith("1. CRITICAL")
    assert item["tags"]["beamreach:honeytoken-id"] == item["honeytoken_id"]
    assert item["name"] and item["material"]


def test_unknown_class_is_rejected():
    with pytest.raises(ValueError, match="unknown honeytoken class"):
        catalog.new_honeytoken_item(workspace_id="ws-1", honeytoken_class="nope")


def test_bad_placement_type_is_rejected():
    with pytest.raises(ValueError, match="placement.type"):
        catalog.normalize_placement({"type": "somewhere"})


@mock_aws
def test_create_stores_the_record_and_plants_through_the_queue(local_workspace):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="items",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    planted = []
    inline = queue.InlineQueue(lambda captain, command, payload: planted.append((captain, command, payload)) or {"pr_url": "https://github.com/o/r/pull/9"})
    queue.set_job_queue(lambda: inline)

    out = hp_commands.create({"workspace_id": "ws-1", "honeytoken_class": "aws_credential", "name": "svc_backup", "region": "us-east-1"})

    assert out["name"] == "svc_backup" and out["status"] == "pending_pr"
    assert out["plant_job"]["status"] == "completed" and out["plant_job"]["result_summary"]["pr_url"].endswith("/pull/9")
    (call,) = planted
    assert call[:2] == ("honeypot", "plant")
    assert call[2]["honeytoken_id"] == out["honeytoken_id"] and call[2]["workspace_id"] == "ws-1" and call[2]["org_id"] == "org-1"
    stored = ddb.Table("items").get_item(Key={"pk": workspace_pk("ws-1"), "sk": honeytoken_sort_key(out["honeytoken_id"])})["Item"]
    assert stored["honeytoken_class"] == "aws_credential" and stored["status"] == "pending_pr"


@mock_aws
def test_create_without_planting(local_workspace):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="items",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    queue.set_job_queue(lambda: queue.InlineQueue(lambda *a: pytest.fail("must not plant")))
    out = hp_commands.create({"workspace_id": "ws-1", "honeytoken_class": "s3_object", "plant": False})
    assert "plant_job" not in out and out["honeytoken_class"] == "s3_object"
