"""The map on DynamoDB, behind the GraphStore interface: every install before 0.6
has its map here, and it stays the rollback target once SQLite is the default.

It wraps ``InfraMapStore`` (the table layout every existing map uses) and adds
nothing to the table: the paged, filtered and graph-shaped reads load the
whole graph and answer in memory, as the routes did before the interface
existed. A scan writes the way the mapper always did: every node and edge is
put, then what the previous graph had and this scan did not is deleted. There
is no history on this backend (docs/specs/map-store.md, open decision 2):
``list_scans`` is empty and ``diff``/``prune`` say so.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from infra_map.graph_store import (
    DEFAULT_EDGE_PAGE,
    DEFAULT_NEIGHBOURHOOD,
    DEFAULT_PAGE,
    MAX_DEPTH,
    MAX_EDGES_OF,
    Diff,
    Direction,
    EdgeQuery,
    NodeQuery,
    Page,
    Path,
    Scan,
    Subgraph,
    Summary,
    edge_sort_key,
    node_sort_key,
    page_in_memory,
    paths_in_memory,
    query_key,
    summarize_in_memory,
)
from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.store import InfraMapStore

logger = logging.getLogger("infra_map.dynamo_store")


class DynamoGraphStore:
    def __init__(self, table_name: str, workspace_id: str, *, session: Any = None, table: Any = None, client: Any = None):
        self.inner = InfraMapStore(table_name=table_name, workspace_id=workspace_id, session=session, table=table, client=client)
        self.workspace_id = self.inner.workspace_id
        self.table_name = table_name

    # ── point reads ──────────────────────────────────────────────────────────
    def get_node(self, org_id: str, node_id: str, *, as_of: Optional[int] = None) -> Optional[ResourceNode]:
        return self.inner.get_resource(org_id, node_id)

    def get_nodes(self, org_id: str, node_ids: Sequence[str], *, as_of: Optional[int] = None) -> Dict[str, ResourceNode]:
        return self.inner.batch_get_resources(org_id, list(node_ids))

    # ── paged reads: the whole graph, filtered here ──────────────────────────
    def query_nodes(self, org_id: str, query: NodeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE,
                    as_of: Optional[int] = None) -> Page[ResourceNode]:
        nodes = [n for n in self.inner.fetch_graph(org_id)["nodes"] if query.matches(n)]
        return page_in_memory(nodes, sort_key=node_sort_key, cursor=cursor, limit=limit, key=query_key(query, org=org_id, kind="nodes"))

    def query_edges(self, org_id: str, query: EdgeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_EDGE_PAGE,
                    as_of: Optional[int] = None) -> Page[RelationshipEdge]:
        edges = [e for e in self.inner.fetch_graph(org_id)["edges"] if query.matches(e)]
        return page_in_memory(edges, sort_key=edge_sort_key, cursor=cursor, limit=limit, key=query_key(query, org=org_id, kind="edges"))

    # ── graph reads ──────────────────────────────────────────────────────────
    def edges_of(self, org_id: str, node_id: str, *, direction: Direction = "both", types: Optional[Sequence[str]] = None,
                 limit: int = MAX_EDGES_OF, as_of: Optional[int] = None) -> List[RelationshipEdge]:
        out: List[RelationshipEdge] = []
        if direction in ("outgoing", "both"):
            out += self.inner.list_outgoing_relationships(org_id, node_id, relationship_types=types)
        if direction in ("incoming", "both"):
            out += self.inner.list_incoming_relationships(org_id, node_id, relationship_types=types)
        return out[:limit]

    def neighborhood(self, org_id: str, node_id: str, *, depth: int = 1, direction: Direction = "both",
                     types: Optional[Sequence[str]] = None, max_nodes: int = DEFAULT_NEIGHBOURHOOD, induced: bool = False,
                     as_of: Optional[int] = None) -> Subgraph:
        found = self.inner.neighbors(org_id, node_id, depth=depth, direction=direction, relationship_types=types)
        nodes: List[ResourceNode] = list(found["nodes"])
        edges: List[RelationshipEdge] = list(found["edges"])
        truncated = False
        if len(nodes) > max_nodes:
            # the focus first, then by depth is what the walk gives; keep the closest
            keep = {node_id} | {n.resource_id for n in nodes[: max_nodes - 1] if n.resource_id != node_id}
            nodes = [n for n in nodes if n.resource_id in keep]
            edges = [e for e in edges if e.from_resource_id in keep and e.to_resource_id in keep]
            truncated = True
        if induced:
            ids = {n.resource_id for n in nodes}
            seen = {e.canonical_edge_id() for e in edges}
            for node in nodes:
                for edge in self.inner.list_outgoing_relationships(org_id, node.resource_id, relationship_types=types):
                    if edge.to_resource_id in ids and edge.canonical_edge_id() not in seen:
                        seen.add(edge.canonical_edge_id())
                        edges.append(edge)
        return Subgraph(focus=node_id, nodes=nodes, edges=edges, truncated=truncated)

    def paths(self, org_id: str, from_id: str, to_id: str, *, max_depth: int = MAX_DEPTH, types: Optional[Sequence[str]] = None,
              limit: int = 5, as_of: Optional[int] = None) -> List[Path]:
        return paths_in_memory(from_id, to_id, self.inner.fetch_graph(org_id)["edges"], max_depth=max_depth, types=types, limit=limit)

    def summary(self, org_id: str, *, as_of: Optional[int] = None) -> Summary:
        graph = self.inner.fetch_graph(org_id)
        return summarize_in_memory(graph["nodes"], graph["edges"], scan=None)

    # ── writes ───────────────────────────────────────────────────────────────
    def begin_scan(self, org_id: str, *, scopes: Optional[Sequence[Tuple[str, str]]] = None, job_id: Optional[str] = None) -> "_DynamoScan":
        return _DynamoScan(self.inner, org_id, scopes=list(scopes) if scopes else None, job_id=job_id)

    # ── history: none here ───────────────────────────────────────────────────
    def list_scans(self, org_id: str, *, limit: int = 50) -> List[Scan]:
        return []

    def diff(self, org_id: str, from_scan: int, to_scan: int, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE) -> Diff:
        raise NotImplementedError("map history needs the sqlite store (map.store = sqlite)")

    def prune(self, org_id: str, *, keep_days: int, keep_scans: int) -> int:
        raise NotImplementedError("map history needs the sqlite store (map.store = sqlite)")

    # ── compatibility ────────────────────────────────────────────────────────
    def fetch_graph(self, org_id: str, *, limit: Optional[int] = None) -> Dict[str, List]:
        return self.inner.fetch_graph(org_id, limit=limit)

    def close(self) -> None:
        return None


class _DynamoScan:
    """The mapper's write block as it has always been: read what is there, put every
    node and edge of this scan, delete what this scan did not produce (within its
    scopes, when it has any). Nothing is visible to readers atomically; DynamoDB has no
    transaction of this size."""

    def __init__(self, inner: InfraMapStore, org_id: str, *, scopes: Optional[List[Tuple[str, str]]], job_id: Optional[str]):
        self.inner = inner
        self.org_id = org_id
        self.scopes = scopes
        self.nodes: Dict[str, ResourceNode] = {}
        self.edges: Dict[str, RelationshipEdge] = {}
        self.scan = Scan(scan_id=0, org_id=org_id, started_at=int(time.time() * 1000), scopes=scopes, job_id=job_id)
        self.done = False

    def put_nodes(self, nodes: Iterable[ResourceNode]) -> None:
        for node in nodes:
            self.nodes[node.resource_id] = node

    def put_edges(self, edges: Iterable[RelationshipEdge]) -> None:
        for edge in edges:
            self.edges[edge.canonical_edge_id()] = edge

    def _in_scope(self, item: Any) -> bool:
        return self.scopes is None or (item.source or "", item.scope or "") in set(self.scopes)

    def commit(self) -> Scan:
        existing = self.inner.fetch_graph(self.org_id)
        existing_nodes = {n.resource_id: n for n in existing.get("nodes", [])}
        existing_edges = {e.canonical_edge_id(): e for e in existing.get("edges", [])}
        for node in self.nodes.values():
            self.inner.put_resource(node)
        for edge in self.edges.values():
            self.inner.put_relationship(edge)
        stale_nodes = [n for rid, n in existing_nodes.items() if rid not in self.nodes and self._in_scope(n)]
        stale_edges = [e for eid, e in existing_edges.items() if eid not in self.edges and self._in_scope(e)]
        for node in stale_nodes:
            self.inner.delete_resource(self.org_id, node.resource_id)
        for edge in stale_edges:
            self.inner.delete_relationship(edge)
        if stale_nodes or stale_edges:
            logger.info("removed %d stale nodes and %d stale edges from the map", len(stale_nodes), len(stale_edges))
        self.scan.finished_at = int(time.time() * 1000)
        self.scan.status = "completed"
        self.scan.node_count = len(self.nodes)
        self.scan.edge_count = len(self.edges)
        self.scan.added = len(set(self.nodes) - set(existing_nodes)) + len(set(self.edges) - set(existing_edges))
        self.scan.removed = len(stale_nodes) + len(stale_edges)
        self.done = True
        return self.scan

    def abort(self, error: Optional[str] = None) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.scan.status = "aborted"
        self.scan.error = error
        self.done = True

    def __enter__(self) -> "_DynamoScan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.done:
            return
        if exc_type is None:
            self.commit()
        else:
            self.abort(str(exc))
