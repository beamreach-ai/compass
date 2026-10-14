from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import boto3
from boto3.dynamodb.conditions import Key

from compass_core.keys import (
    PK_NAME,
    SK_NAME,
    map_fact_scope_prefix,
    map_fact_sort_key,
    normalize_workspace_id,
    workspace_pk,
)
from infra_map.facts import EdgeFact, NodeFact


class InfraMapFactStore:
    """Persist source-owned infra_map facts in the shared map table."""

    def __init__(
        self,
        table_name: str,
        workspace_id: str,
        session: Optional[boto3.Session] = None,
        *,
        table=None,
    ):
        self.table_name = table_name
        self.workspace_id = normalize_workspace_id(workspace_id)
        self._session = session or boto3.Session()
        if table is not None:
            self.table = table
        else:
            dynamo = self._session.resource("dynamodb")
            self.table = dynamo.Table(table_name)

    def replace_scope(
        self,
        org_id: str,
        *,
        source: str,
        scope: str,
        node_facts: Sequence[NodeFact],
        edge_facts: Sequence[EdgeFact],
    ) -> Tuple[int, int]:
        deleted_nodes = 0
        deleted_edges = 0
        existing = self.list_scope_facts(org_id, source=source, scope=scope)
        for item in existing.get("items", []):
            self.table.delete_item(
                Key={
                    PK_NAME: workspace_pk(self.workspace_id),
                    SK_NAME: item[SK_NAME],
                }
            )
            if item.get("fact_kind") == "node_claim":
                deleted_nodes += 1
            elif item.get("fact_kind") == "edge_claim":
                deleted_edges += 1

        for node_fact in node_facts:
            self.put_node_fact(source=source, scope=scope, fact=node_fact)
        for edge_fact in edge_facts:
            self.put_edge_fact(source=source, scope=scope, fact=edge_fact)
        return deleted_nodes, deleted_edges

    def list_scope_facts(
        self,
        org_id: str,
        *,
        source: str,
        scope: str,
    ) -> Dict[str, List]:
        response = self.table.query(
            KeyConditionExpression=Key(PK_NAME).eq(workspace_pk(self.workspace_id))
            & Key(SK_NAME).begins_with(map_fact_scope_prefix(org_id, source, scope))
        )
        items = response.get("Items", [])
        node_facts = [NodeFact.from_item(item) for item in items if item.get("fact_kind") == "node_claim"]
        edge_facts = [EdgeFact.from_item(item) for item in items if item.get("fact_kind") == "edge_claim"]
        return {"node_facts": node_facts, "edge_facts": edge_facts, "items": items}

    def put_node_fact(self, *, source: str, scope: str, fact: NodeFact) -> Dict:
        item = fact.to_item(source=source, scope=scope)
        item[PK_NAME] = workspace_pk(self.workspace_id)
        item[SK_NAME] = map_fact_sort_key(fact.org_id, source, scope, "NODE", fact.canonical_fact_id())
        item["workspace_id"] = self.workspace_id
        self.table.put_item(Item=item)
        return item

    def put_edge_fact(self, *, source: str, scope: str, fact: EdgeFact) -> Dict:
        item = fact.to_item(source=source, scope=scope)
        item[PK_NAME] = workspace_pk(self.workspace_id)
        item[SK_NAME] = map_fact_sort_key(fact.org_id, source, scope, "EDGE", fact.canonical_fact_id())
        item["workspace_id"] = self.workspace_id
        self.table.put_item(Item=item)
        return item
