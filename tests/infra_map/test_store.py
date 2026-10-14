from datetime import datetime, timezone
from unittest.mock import MagicMock

from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.store import InfraMapStore


def _sample_node(resource_id: str, name: str) -> ResourceNode:
    return ResourceNode(
        org_id="beamreach",
        resource_id=resource_id,
        type="lambda",
        account_id="123456789012",
        region="eu-west-1",
        name=name,
        last_seen=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _sample_edge(from_id: str, to_id: str, rel_type: str = "uses_in") -> RelationshipEdge:
    return RelationshipEdge(
        org_id="beamreach",
        from_resource_id=from_id,
        to_resource_id=to_id,
        relationship_type=rel_type,
        confidence="high",
        evidence=["unit-test"],
        last_seen=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _store():
    table = MagicMock()
    client = MagicMock()
    store = InfraMapStore(table_name="beamreach_map_demo", workspace_id="workspace-123", table=table, client=client)
    return store, table, client


def test_put_resource_writes_expected_item():
    store, table, _ = _store()
    node = _sample_node("aws:lambda:demo", "demo")

    store.put_resource(node)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["resource_id"] == "aws:lambda:demo"
    assert item["resource_type"] == "lambda"
    assert item["pk"] == "WORKSPACE#workspace-123"
    assert item["sk"] == "MAP#ORG#beamreach#NODE#aws:lambda:demo"


def test_put_relationship_uses_prefixed_sort_key():
    store, table, _ = _store()
    edge = _sample_edge("from", "to")

    store.put_relationship(edge)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["resource_id"].startswith("MAP#ORG#beamreach#REL#FROM#from#")
    assert item["relationship_type"] == "uses_in"
    assert item["target_resource_id"] == "to"


def test_list_outgoing_relationships_returns_edges():
    store, table, _ = _store()
    edge = _sample_edge("from", "to", "depends_on")
    payload = edge.to_attributes()
    payload["resource_id"] = "MAP#ORG#beamreach#REL#FROM#from#" + edge.canonical_edge_id()
    table.query.return_value = {"Items": [payload]}

    results = store.list_outgoing_relationships("beamreach", "from", relationship_types=["depends_on"])

    assert len(results) == 1
    assert results[0].relationship_type == "depends_on"
    assert results[0].to_resource_id == "to"


def test_neighbors_collects_nodes_and_edges():
    store, table, client = _store()

    outgoing_edge = _sample_edge("focus", "out-node", "uses_in")
    outgoing_payload = outgoing_edge.to_attributes()
    outgoing_payload["resource_id"] = "MAP#ORG#beamreach#REL#FROM#focus#" + outgoing_edge.canonical_edge_id()

    incoming_edge = _sample_edge("in-node", "focus", "depends_on")
    incoming_payload = incoming_edge.to_attributes()
    incoming_payload["resource_id"] = "MAP#ORG#beamreach#REL#FROM#in-node#" + incoming_edge.canonical_edge_id()

    table.query.side_effect = [
        {"Items": [outgoing_payload]},  # outgoing
        {"Items": [incoming_payload]},  # incoming
    ]

    focus_node = _sample_node("focus", "focus")
    out_node = _sample_node("out-node", "out")
    in_node = _sample_node("in-node", "in")
    client.batch_get_item.return_value = {
        "Responses": {
            "beamreach_map_demo": [
                focus_node.to_item(),
                out_node.to_item(),
                in_node.to_item(),
            ]
        }
    }

    result = store.neighbors("beamreach", "focus", depth=1)

    assert result["focus"] == "focus"
    assert len(result["edges"]) == 2
    relationship_types = {edge.relationship_type for edge in result["edges"]}
    assert relationship_types == {"uses_in", "depends_on"}
    node_ids = {node.resource_id for node in result["nodes"]}
    assert node_ids == {"focus", "out-node", "in-node"}
