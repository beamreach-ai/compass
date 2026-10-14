from __future__ import annotations

from typing import Dict, List, Tuple

from infra_map.facts import EdgeFact, FactBatch, NodeFact
from infra_map.models import RelationshipEdge, ResourceNode


class InfraMapMaterializer:
    """Convert fact batches into the current graph view."""

    def materialize_batch(self, batch: FactBatch) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        nodes: Dict[str, ResourceNode] = {}
        edges: Dict[Tuple[str, str, str], RelationshipEdge] = {}

        for node_fact in batch.node_facts:
            node = self.materialize_node_fact(node_fact, source=batch.source, scope=batch.scope)
            nodes[node.resource_id] = node

        for edge_fact in batch.edge_facts:
            edge = self.materialize_edge_fact(edge_fact, source=batch.source, scope=batch.scope)
            edges[(edge.from_resource_id, edge.to_resource_id, edge.relationship_type)] = edge

        return list(nodes.values()), list(edges.values())

    def materialize_node_fact(self, fact: NodeFact, *, source: str, scope: str) -> ResourceNode:
        return ResourceNode(
            org_id=fact.org_id,
            resource_id=fact.entity_id,
            type=fact.node_type,
            account_id=fact.account_id,
            region=fact.region,
            name=fact.name,
            tags=dict(fact.tags),
            attributes=dict(fact.attributes),
            node_class=fact.node_class,
            canonical_ref=fact.canonical_ref,
            aliases=list(fact.aliases),
            confidence=fact.confidence,
            source=source,
            scope=scope,
            last_seen=fact.observed_at,
            expires_at=fact.expires_at,
        )

    def materialize_edge_fact(self, fact: EdgeFact, *, source: str, scope: str) -> RelationshipEdge:
        return RelationshipEdge(
            org_id=fact.org_id,
            from_resource_id=fact.from_entity_id,
            to_resource_id=fact.to_entity_id,
            relationship_type=fact.edge_type,
            confidence=fact.confidence,
            evidence=list(fact.evidence),
            attributes=dict(fact.attributes),
            source=source,
            scope=scope,
            last_seen=fact.observed_at,
            expires_at=fact.expires_at,
        )
