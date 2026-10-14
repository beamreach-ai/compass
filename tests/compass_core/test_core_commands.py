import boto3
from moto import mock_aws

from compass_core import commands

CONFIG = {
    "dynamodb_config": {
        "workspace_id": "local",
        "audit": "compass_items_local",
        "map": "compass_map_local",
        "aws_inventory": "compass_aws_inventory_local",
        "tf_inventory": "compass_tf_inventory_local",
        "tf_state": "compass_tf_state_local",
        "progress": "compass_progress_local",
    }
}


def test_specs_cover_the_open_tables_once_each():
    names = [s["TableName"] for s in commands.table_specs(CONFIG)]
    assert sorted(names) == sorted(set(names))
    assert set(names) == {
        "compass_items_local", "compass_map_local", "compass_aws_inventory_local",
        "compass_tf_inventory_local", "compass_tf_state_local", "compass_progress_local",
    }
    by_name = {s["TableName"]: s for s in commands.table_specs(CONFIG)}
    assert {i["IndexName"] for i in by_name["compass_map_local"]["GlobalSecondaryIndexes"]} == {"ResourceTypeIndex", "TargetResourceIndex"}
    assert by_name["compass_map_local"]["Ttl"] == "expires_at"
    assert "GlobalSecondaryIndexes" not in by_name["compass_items_local"]


@mock_aws
def test_create_tables_is_idempotent():
    client = boto3.client("dynamodb", region_name="us-east-1")
    first = commands.create_tables(CONFIG, client=client)
    assert len(first["created"]) == 6 and first["present"] == []
    second = commands.create_tables(CONFIG, client=client)
    assert second["created"] == [] and len(second["present"]) == 6
    ttl = client.describe_time_to_live(TableName="compass_map_local")["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "expires_at"
