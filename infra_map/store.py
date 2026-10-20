from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import boto3
from boto3.dynamodb.conditions import Key

from compass_core import sessions
from compass_core.keys import (
    PK_NAME,
    SK_NAME,
    map_edge_prefix,
    map_edge_sort_key,
    map_graph_prefix,
    map_node_sort_key,
    map_target_index_pk,
    map_target_index_sk,
    map_type_index_pk,
    map_type_index_sk,
    normalize_workspace_id,
    workspace_pk,
)

from .models import RelationshipEdge, ResourceNode


def _chunked(iterable: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(iterable), size):
        yield iterable[idx : idx + size]


class InfraMapStore:
    """Typed helper around the DynamoDB infra map table."""

    TARGET_INDEX_NAME = "TargetResourceIndex"

    def __init__(
        self,
        table_name: str,
        workspace_id: str,
        session: Optional[boto3.Session] = None,
        *,
        table=None,
        client=None,
    ):
        self.table_name = table_name
        self.workspace_id = normalize_workspace_id(workspace_id)
        self._session = session or sessions.control()
        if table is not None:
            self.table = table
        else:
            dynamo = self._session.resource("dynamodb")
            self.table = dynamo.Table(table_name)
        if client is not None:
            self._client = client
        else:
            meta = getattr(self.table, "meta", None)
            self._client = getattr(meta, "client", None) or self._session.client("dynamodb")

    # -------------------------------------------------------------------------
    # Resource helpers
    # -------------------------------------------------------------------------
    def put_resource(self, resource: ResourceNode) -> Dict:
        item = resource.to_item()
        item[PK_NAME] = workspace_pk(self.workspace_id)
        item[SK_NAME] = map_node_sort_key(resource.org_id, resource.resource_id)
        item["workspace_id"] = self.workspace_id
        item["gsi1pk"] = map_type_index_pk(self.workspace_id, resource.org_id, resource.type)
        item["gsi1sk"] = map_type_index_sk(resource.resource_id)
        self.table.put_item(Item=item)
        return item

    def get_resource(self, org_id: str, resource_id: str) -> Optional[ResourceNode]:
        resp = self.table.get_item(
            Key={
                PK_NAME: workspace_pk(self.workspace_id),
                SK_NAME: map_node_sort_key(org_id, resource_id),
            }
        )
        item = resp.get("Item")
        if not item:
            return None
        return ResourceNode.from_item(item)

    def delete_resource(self, org_id: str, resource_id: str) -> None:
        self.table.delete_item(
            Key={
                PK_NAME: workspace_pk(self.workspace_id),
                SK_NAME: map_node_sort_key(org_id, resource_id),
            }
        )

    # -------------------------------------------------------------------------
    # Relationship helpers
    # -------------------------------------------------------------------------
    def put_relationship(self, edge: RelationshipEdge) -> Dict:
        item = edge.to_attributes()
        item["workspace_id"] = self.workspace_id
        item[PK_NAME] = workspace_pk(self.workspace_id)
        item[SK_NAME] = self._edge_sort_key(edge)
        item["resource_id"] = item[SK_NAME]
        item["gsi2pk"] = map_target_index_pk(self.workspace_id, edge.org_id, edge.to_resource_id)
        item["gsi2sk"] = map_target_index_sk(edge.relationship_type, edge.canonical_edge_id())
        item["gsi1pk"] = map_type_index_pk(self.workspace_id, edge.org_id, "relationship")
        item["gsi1sk"] = map_type_index_sk(edge.canonical_edge_id())
        self.table.put_item(Item=item)
        return item

    def delete_relationship(self, edge: RelationshipEdge) -> None:
        sort_key = self._edge_sort_key(edge)
        self.table.delete_item(
            Key={
                PK_NAME: workspace_pk(self.workspace_id),
                SK_NAME: sort_key,
            }
        )

    def list_outgoing_relationships(
        self,
        org_id: str,
        resource_id: str,
        *,
        relationship_types: Optional[Sequence[str]] = None,
    ) -> List[RelationshipEdge]:
        prefix = self._edge_prefix(org_id, resource_id)
        response = self.table.query(
            KeyConditionExpression=Key(PK_NAME).eq(workspace_pk(self.workspace_id)) & Key(SK_NAME).begins_with(prefix)
        )
        edges = [RelationshipEdge.from_item(item) for item in response.get("Items", [])]
        if relationship_types:
            allowed = set(relationship_types)
            edges = [edge for edge in edges if edge.relationship_type in allowed]
        return edges

    def list_incoming_relationships(
        self,
        org_id: str,
        resource_id: str,
        *,
        relationship_types: Optional[Sequence[str]] = None,
    ) -> List[RelationshipEdge]:
        key_condition = Key("gsi2pk").eq(map_target_index_pk(self.workspace_id, org_id, resource_id))
        index_kwargs = {"IndexName": self.TARGET_INDEX_NAME}
        if relationship_types and len(relationship_types) == 1:
            key_condition = key_condition & Key("gsi2sk").begins_with(f"RELTYPE#{relationship_types[0]}#")
            remaining_filters: Optional[Set[str]] = None
        else:
            remaining_filters = set(relationship_types) if relationship_types else None

        response = self.table.query(KeyConditionExpression=key_condition, **index_kwargs)
        edges = [RelationshipEdge.from_item(item) for item in response.get("Items", [])]
        if remaining_filters:
            edges = [edge for edge in edges if edge.relationship_type in remaining_filters]
        return [edge for edge in edges if edge.org_id == org_id]

    def neighbors(
        self,
        org_id: str,
        resource_id: str,
        *,
        depth: int = 1,
        direction: str = "both",
        relationship_types: Optional[Sequence[str]] = None,
    ) -> Dict[str, List]:
        if depth < 1:
            return {"focus": resource_id, "nodes": [], "edges": []}
        direction = direction.lower()
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be 'incoming', 'outgoing', or 'both'")

        queue: deque = deque([(resource_id, 0)])
        seen_nodes: Set[str] = {resource_id}
        collected: Dict[str, RelationshipEdge] = {}

        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue

            if direction in {"outgoing", "both"}:
                for edge in self.list_outgoing_relationships(
                    org_id,
                    current,
                    relationship_types=relationship_types,
                ):
                    key = f"out:{edge.canonical_edge_id()}"
                    collected[key] = edge
                    neighbor = edge.to_resource_id
                    if neighbor not in seen_nodes:
                        seen_nodes.add(neighbor)
                        queue.append((neighbor, level + 1))

            if direction in {"incoming", "both"}:
                for edge in self.list_incoming_relationships(
                    org_id,
                    current,
                    relationship_types=relationship_types,
                ):
                    key = f"in:{edge.canonical_edge_id()}"
                    collected[key] = edge
                    neighbor = edge.from_resource_id
                    if neighbor not in seen_nodes:
                        seen_nodes.add(neighbor)
                        queue.append((neighbor, level + 1))

        nodes = self.batch_get_resources(org_id, list(seen_nodes))
        return {
            "focus": resource_id,
            "nodes": list(nodes.values()),
            "edges": list(collected.values()),
        }

    # -------------------------------------------------------------------------
    # Batch helpers
    # -------------------------------------------------------------------------
    def batch_get_resources(self, org_id: str, resource_ids: Sequence[str]) -> Dict[str, ResourceNode]:
        if not resource_ids:
            return {}

        resources: Dict[str, ResourceNode] = {}
        for chunk in _chunked(list(dict.fromkeys(resource_ids)), 100):
            keys = [
                {
                    PK_NAME: workspace_pk(self.workspace_id),
                    SK_NAME: map_node_sort_key(org_id, rid),
                }
                for rid in chunk
            ]
            request = {self.table_name: {"Keys": keys}}
            response = self._client.batch_get_item(RequestItems=request)
            items = response.get("Responses", {}).get(self.table_name, [])
            for item in items:
                node = ResourceNode.from_item(item)
                resources[node.resource_id] = node

            unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name, {}).get("Keys", [])
            retries = 0
            while unprocessed and retries < 5:
                retry_resp = self._client.batch_get_item(
                    RequestItems={self.table_name: {"Keys": unprocessed}}
                )
                items = retry_resp.get("Responses", {}).get(self.table_name, [])
                for item in items:
                    node = ResourceNode.from_item(item)
                    resources[node.resource_id] = node
                unprocessed = (
                    retry_resp.get("UnprocessedKeys", {}).get(self.table_name, {}).get("Keys", [])
                )
                retries += 1
        return resources

    def fetch_graph(
        self,
        org_id: str,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, List]:
        nodes: Dict[str, ResourceNode] = {}
        edges: Dict[str, RelationshipEdge] = {}

        exclusive_key = None
        fetched = 0

        while True:
            query_kwargs = {
                "KeyConditionExpression": Key(PK_NAME).eq(workspace_pk(self.workspace_id))
                & Key(SK_NAME).begins_with(map_graph_prefix(org_id)),
            }
            if exclusive_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_key

            response = self.table.query(**query_kwargs)
            items = response.get("Items", [])

            for item in items:
                record_type = item.get("record_type") or item.get("resource_type")
                if record_type == "relationship":
                    edge = RelationshipEdge.from_item(item)
                    edges[edge.canonical_edge_id()] = edge
                else:
                    node = ResourceNode.from_item(item)
                    nodes[node.resource_id] = node

                fetched += 1
                if limit is not None and fetched >= limit:
                    break

            if limit is not None and fetched >= limit:
                break

            exclusive_key = response.get("LastEvaluatedKey")
            if not exclusive_key:
                break

        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    def replace_scope(
        self,
        org_id: str,
        *,
        source: str,
        scope: str,
        nodes: Sequence[ResourceNode],
        edges: Sequence[RelationshipEdge],
    ) -> Tuple[int, int]:
        """Replace all facts owned by ``source`` and ``scope`` with the provided ones."""
        deleted_nodes = 0
        deleted_edges = 0
        existing = self.fetch_graph(org_id)
        for node in existing.get("nodes", []):
            if node.source == source and node.scope == scope:
                self.delete_resource(org_id, node.resource_id)
                deleted_nodes += 1
        for edge in existing.get("edges", []):
            if edge.source == source and edge.scope == scope:
                self.delete_relationship(edge)
                deleted_edges += 1
        for node in nodes:
            self.put_resource(node)
        for edge in edges:
            self.put_relationship(edge)
        return deleted_nodes, deleted_edges

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _edge_sort_key(self, edge: RelationshipEdge) -> str:
        return map_edge_sort_key(edge.org_id, edge.from_resource_id, edge.canonical_edge_id())

    def _edge_prefix(self, org_id: str, resource_id: str) -> str:
        return map_edge_prefix(org_id, resource_id)
