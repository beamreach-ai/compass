from datetime import datetime, timezone
from unittest.mock import MagicMock

from infra_map.fact_store import InfraMapFactStore
from infra_map.facts import EdgeFact, NodeFact


def _store():
    table = MagicMock()
    store = InfraMapFactStore(table_name="beamreach_map_demo", workspace_id="workspace-123", table=table)
    return store, table


def test_put_node_fact_writes_fact_item():
    store, table = _store()
    fact = NodeFact(
        org_id="beamreach",
        entity_id="tfstate://s3/states/demo.tfstate",
        node_type="tf_state_file",
        account_id="",
        region="global",
        name="demo.tfstate",
        observed_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
    )

    store.put_node_fact(source="tf_state", scope="state_file:demo", fact=fact)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "WORKSPACE#workspace-123"
    assert item["sk"].startswith("MAP#FACT#ORG#beamreach#SOURCE#tf_state#SCOPE#state_file:demo#KIND#NODE#FACT#")
    assert item["fact_kind"] == "node_claim"
    assert item["entity_id"] == "tfstate://s3/states/demo.tfstate"


def test_replace_scope_deletes_existing_facts_before_writing():
    store, table = _store()
    table.query.return_value = {
        "Items": [
            {
                "pk": "WORKSPACE#workspace-123",
                "sk": "MAP#FACT#ORG#beamreach#SOURCE#tf_state#SCOPE#state_file:demo#KIND#NODE#FACT#old-node",
                "fact_kind": "node_claim",
                "org_id": "beamreach",
                "entity_id": "node-1",
                "node_type": "tf_state_file",
                "account_id": "",
                "region": "global",
                "name": "demo.tfstate",
                "observed_at": "2026-04-04T00:00:00Z",
            },
            {
                "pk": "WORKSPACE#workspace-123",
                "sk": "MAP#FACT#ORG#beamreach#SOURCE#tf_state#SCOPE#state_file:demo#KIND#EDGE#FACT#old-edge",
                "fact_kind": "edge_claim",
                "org_id": "beamreach",
                "from_entity_id": "node-1",
                "to_entity_id": "node-2",
                "edge_type": "contains",
                "observed_at": "2026-04-04T00:00:00Z",
            },
        ]
    }

    node_fact = NodeFact(
        org_id="beamreach",
        entity_id="node-2",
        node_type="tf_state_instance",
        account_id="",
        region="global",
        name="module.demo.aws_lambda_function.example",
    )
    edge_fact = EdgeFact(
        org_id="beamreach",
        from_entity_id="node-1",
        to_entity_id="node-2",
        edge_type="contains",
    )

    deleted_nodes, deleted_edges = store.replace_scope(
        "beamreach",
        source="tf_state",
        scope="state_file:demo",
        node_facts=[node_fact],
        edge_facts=[edge_fact],
    )

    assert deleted_nodes == 1
    assert deleted_edges == 1
    assert table.delete_item.call_count == 2
    assert table.put_item.call_count == 2
