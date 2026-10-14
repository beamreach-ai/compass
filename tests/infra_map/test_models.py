from datetime import datetime, timezone

from infra_map.models import RelationshipEdge, ResourceNode, TerraformMetadata


def test_resource_node_round_trip():
    ts = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    node = ResourceNode(
        org_id="beamreach",
        resource_id="aws:secretsmanager:eu-west-1:123:secret:demo",
        type="secretsmanager_secret",
        account_id="123",
        region="eu-west-1",
        name="demo",
        tags={"env": "demo"},
        attributes={"key": "value"},
        last_seen=ts,
        terraform=TerraformMetadata(
            resource_address="module.secrets.aws_secretsmanager_secret.demo",
            repo="beamreach/infra",
            state_file="demo.tfstate",
        ),
        expires_at=1234567890,
    )

    item = node.to_item()
    assert item["resource_id"] == node.resource_id
    assert item["terraform"]["resource_address"] == "module.secrets.aws_secretsmanager_secret.demo"

    hydrated = ResourceNode.from_item(item)
    assert hydrated.org_id == node.org_id
    assert hydrated.name == "demo"
    assert hydrated.tags["env"] == "demo"
    assert hydrated.attributes["key"] == "value"
    assert hydrated.last_seen == ts
    assert hydrated.expires_at == 1234567890


def test_relationship_edge_round_trip():
    ts = datetime(2025, 1, 2, 15, 30, tzinfo=timezone.utc)
    edge = RelationshipEdge(
        org_id="beamreach",
        from_resource_id="aws:lambda:fn",
        to_resource_id="aws:secretsmanager:secret",
        relationship_type="uses_in",
        confidence="high",
        evidence=["lambda:GetFunction"],
        attributes={"reason": "env var"},
        last_seen=ts,
        expires_at=123,
    )

    payload = edge.to_attributes()
    payload["resource_id"] = "REL#FROM#aws:lambda:fn#" + edge.canonical_edge_id()
    assert payload["target_resource_id"] == edge.to_resource_id

    hydrated = RelationshipEdge.from_item(payload)
    assert hydrated.org_id == edge.org_id
    assert hydrated.from_resource_id == edge.from_resource_id
    assert hydrated.to_resource_id == edge.to_resource_id
    assert hydrated.relationship_type == "uses_in"
    assert hydrated.confidence == "high"
    assert hydrated.evidence == ["lambda:GetFunction"]
    assert hydrated.attributes["reason"] == "env var"
    assert hydrated.last_seen == ts
