"""The map's store, seen from everything that reads or writes it: one interface,
a registry with an open default, and the types the interface speaks.

A store holds one workspace's graph: nodes with a provider-canonical id, typed
directed edges between them, and the scans that produced them. Reads are
point, paged, or graph-shaped (a neighbourhood, paths); writes happen one scan
at a time through ``begin_scan``. ``open_store`` builds the store a workspace
config names (``map.store``, or ``COMPASS_MAP_STORE`` for one process); a
larger deployment replaces it with ``set_graph_store_factory`` at import time,
the way it replaces the job queue. docs/specs/map-store.md is the design.
"""

from __future__ import annotations

import base64
import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Iterable, List, Literal, Mapping, Optional, Protocol, Sequence, Tuple, TypeVar

from infra_map.models import RelationshipEdge, ResourceNode

Direction = Literal["incoming", "outgoing", "both"]
T = TypeVar("T")

DEFAULT_PAGE = 500
MAX_PAGE = 5000
DEFAULT_EDGE_PAGE = 1000
MAX_EDGE_PAGE = 10000
DEFAULT_NEIGHBOURHOOD = 500
MAX_NEIGHBOURHOOD = 2000
MAX_DEPTH = 6
MAX_EDGES_OF = 5000


class ScanInProgress(RuntimeError):
    """Another scan of this workspace and org is being written."""


class BadCursor(ValueError):
    """A cursor that was not issued for this query."""


@dataclass(frozen=True)
class NodeQuery:
    types: Sequence[str] = ()
    regions: Sequence[str] = ()
    accounts: Sequence[str] = ()
    sources: Sequence[str] = ()
    ids: Sequence[str] = ()
    text: Optional[str] = None                      # name or id contains, case-insensitive
    coverage: Optional[str] = None                  # covered | unmanaged | drifted (attributes.coverage_score / drift_status)
    changed_since: Optional[int] = None             # valid_from > this epoch-ms

    def matches(self, node: ResourceNode) -> bool:
        """The filter applied in Python, for backends without a query planner and for tests."""
        if self.types and node.type not in self.types:
            return False
        if self.regions and node.region not in self.regions:
            return False
        if self.accounts and node.account_id not in self.accounts:
            return False
        if self.sources and (node.source or "") not in self.sources:
            return False
        if self.ids and node.resource_id not in self.ids:
            return False
        if self.text:
            needle = self.text.lower()
            if needle not in node.name.lower() and needle not in node.resource_id.lower():
                return False
        if self.coverage:
            attrs = node.attributes or {}
            score = attrs.get("coverage_score", 0) or 0
            if self.coverage == "covered" and not score >= 1:
                return False
            if self.coverage == "unmanaged" and score >= 1:
                return False
            if self.coverage == "drifted" and attrs.get("drift_status") != "drifted":
                return False
        return True


@dataclass(frozen=True)
class EdgeQuery:
    types: Sequence[str] = ()
    from_ids: Sequence[str] = ()
    to_ids: Sequence[str] = ()
    sources: Sequence[str] = ()

    def matches(self, edge: RelationshipEdge) -> bool:
        if self.types and edge.relationship_type not in self.types:
            return False
        if self.from_ids and edge.from_resource_id not in self.from_ids:
            return False
        if self.to_ids and edge.to_resource_id not in self.to_ids:
            return False
        if self.sources and (edge.source or "") not in self.sources:
            return False
        return True


@dataclass
class Page(Generic[T]):
    items: List[T]
    next_cursor: Optional[str] = None
    total: Optional[int] = None


@dataclass
class Subgraph:
    focus: str
    nodes: List[ResourceNode]
    edges: List[RelationshipEdge]
    truncated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"focus": self.focus, "nodes": self.nodes, "edges": self.edges, "truncated": self.truncated}


@dataclass
class Path:
    nodes: List[str]
    edges: List[RelationshipEdge]


@dataclass
class Scan:
    scan_id: int
    org_id: str
    started_at: int
    finished_at: Optional[int] = None
    status: str = "running"                         # running | completed | failed | aborted
    scopes: Optional[List[Tuple[str, str]]] = None  # None: the whole org
    node_count: int = 0
    edge_count: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    job_id: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {**self.__dict__, "scopes": [list(s) for s in self.scopes] if self.scopes else None}


@dataclass
class Summary:
    scan: Optional[Scan]
    nodes: int
    edges: int
    by_type: Dict[str, int] = field(default_factory=dict)
    by_region: Dict[str, int] = field(default_factory=dict)
    by_account: Dict[str, int] = field(default_factory=dict)
    by_source: Dict[str, int] = field(default_factory=dict)
    edge_types: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {**self.__dict__, "scan": self.scan.as_dict() if self.scan else None}


@dataclass
class Diff:
    from_scan: int
    to_scan: int
    items: List[Dict[str, Any]]
    next_cursor: Optional[str] = None


class ScanWriter(Protocol):
    """One scan's writes. ``commit`` makes them visible and closes what the scan did
    not see within its scopes; ``abort`` leaves the map as it was."""

    scan: Scan

    def put_nodes(self, nodes: Iterable[ResourceNode]) -> None: ...
    def put_edges(self, edges: Iterable[RelationshipEdge]) -> None: ...
    def commit(self) -> Scan: ...
    def abort(self, error: Optional[str] = None) -> None: ...
    def __enter__(self) -> "ScanWriter": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...


class GraphStore(Protocol):
    workspace_id: str

    def get_node(self, org_id: str, node_id: str, *, as_of: Optional[int] = None) -> Optional[ResourceNode]: ...
    def get_nodes(self, org_id: str, node_ids: Sequence[str], *, as_of: Optional[int] = None) -> Dict[str, ResourceNode]: ...
    def query_nodes(self, org_id: str, query: NodeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE,
                    as_of: Optional[int] = None) -> Page[ResourceNode]: ...
    def query_edges(self, org_id: str, query: EdgeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_EDGE_PAGE,
                    as_of: Optional[int] = None) -> Page[RelationshipEdge]: ...
    def edges_of(self, org_id: str, node_id: str, *, direction: Direction = "both", types: Optional[Sequence[str]] = None,
                 limit: int = MAX_EDGES_OF, as_of: Optional[int] = None) -> List[RelationshipEdge]: ...
    def neighborhood(self, org_id: str, node_id: str, *, depth: int = 1, direction: Direction = "both",
                     types: Optional[Sequence[str]] = None, max_nodes: int = DEFAULT_NEIGHBOURHOOD, induced: bool = False,
                     as_of: Optional[int] = None) -> Subgraph: ...
    def paths(self, org_id: str, from_id: str, to_id: str, *, max_depth: int = MAX_DEPTH, types: Optional[Sequence[str]] = None,
              limit: int = 5, as_of: Optional[int] = None) -> List[Path]: ...
    def summary(self, org_id: str, *, as_of: Optional[int] = None) -> Summary: ...
    def begin_scan(self, org_id: str, *, scopes: Optional[Sequence[Tuple[str, str]]] = None, job_id: Optional[str] = None) -> ScanWriter: ...
    def list_scans(self, org_id: str, *, limit: int = 50) -> List[Scan]: ...
    def diff(self, org_id: str, from_scan: int, to_scan: int, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE) -> Diff: ...
    def prune(self, org_id: str, *, keep_days: int, keep_scans: int) -> int: ...
    def fetch_graph(self, org_id: str, *, limit: Optional[int] = None) -> Dict[str, List]: ...
    def close(self) -> None: ...


# ── cursors ──────────────────────────────────────────────────────────────────

def encode_cursor(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), default=str).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *, query_key: str) -> Dict[str, Any]:
    """The payload of a cursor this query issued; ``query_key`` is a digest of the
    filter, so a cursor from another query is refused."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is the same error
        raise BadCursor("cursor does not belong to this query") from exc
    if not isinstance(payload, dict) or payload.get("q") != query_key:
        raise BadCursor("cursor does not belong to this query")
    return payload


def query_key(query: Any, **extra: Any) -> str:
    data = {**(query.__dict__ if hasattr(query, "__dict__") else {}), **extra}
    return json.dumps(data, sort_keys=True, default=str)[:512]


# ── graph algorithms in memory (the DynamoDB backend and the tests) ──────────

def node_sort_key(node: ResourceNode) -> Tuple[str, str, str]:
    return (node.type, node.name, node.resource_id)


def edge_sort_key(edge: RelationshipEdge) -> Tuple[str, str, str, str]:
    return (edge.relationship_type, edge.from_resource_id, edge.to_resource_id, edge.canonical_edge_id())


def page_in_memory(items: List[T], *, sort_key: Callable[[T], Tuple], cursor: Optional[str], limit: int, key: str) -> Page[T]:
    """Keyset pagination over a sorted list: the cursor is the last item's sort tuple."""
    items = sorted(items, key=sort_key)
    start = 0
    if cursor:
        after = tuple(decode_cursor(cursor, query_key=key)["k"])
        start = next((i for i, item in enumerate(items) if tuple(sort_key(item)) > after), len(items))
    chunk = items[start : start + limit]
    next_cursor = encode_cursor({"q": key, "k": list(sort_key(chunk[-1]))}) if start + limit < len(items) and chunk else None
    return Page(items=chunk, next_cursor=next_cursor, total=len(items) if not cursor else None)


def bfs_in_memory(
    focus: str,
    edges: Iterable[RelationshipEdge],
    *,
    depth: int,
    direction: Direction,
    types: Optional[Sequence[str]],
    max_nodes: int,
) -> Tuple[Dict[str, int], Dict[str, RelationshipEdge], bool]:
    """Reached node ids with their depth, the edges walked, and whether ``max_nodes`` cut
    the frontier. Walks a dict-indexed edge list the way the SQL stores walk the table."""
    allowed = set(types) if types else None
    outgoing: Dict[str, List[RelationshipEdge]] = {}
    incoming: Dict[str, List[RelationshipEdge]] = {}
    for edge in edges:
        if allowed and edge.relationship_type not in allowed:
            continue
        outgoing.setdefault(edge.from_resource_id, []).append(edge)
        incoming.setdefault(edge.to_resource_id, []).append(edge)
    reached: Dict[str, int] = {focus: 0}
    walked: Dict[str, RelationshipEdge] = {}
    queue: deque = deque([focus])
    truncated = False
    while queue:
        current = queue.popleft()
        level = reached[current]
        if level >= depth:
            continue
        candidates: List[Tuple[RelationshipEdge, str]] = []
        if direction in ("outgoing", "both"):
            candidates += [(e, e.to_resource_id) for e in outgoing.get(current, [])]
        if direction in ("incoming", "both"):
            candidates += [(e, e.from_resource_id) for e in incoming.get(current, [])]
        for edge, other in sorted(candidates, key=lambda pair: pair[1]):
            if len(reached) >= max_nodes and other not in reached:
                truncated = True
                continue
            walked[edge.canonical_edge_id()] = edge
            if other not in reached:
                reached[other] = level + 1
                queue.append(other)
    return reached, walked, truncated


def paths_in_memory(from_id: str, to_id: str, edges: Iterable[RelationshipEdge], *, max_depth: int, types: Optional[Sequence[str]],
                    limit: int) -> List[Path]:
    """Up to ``limit`` shortest simple outgoing paths, shallowest first."""
    allowed = set(types) if types else None
    outgoing: Dict[str, List[RelationshipEdge]] = {}
    for edge in edges:
        if allowed and edge.relationship_type not in allowed:
            continue
        outgoing.setdefault(edge.from_resource_id, []).append(edge)
    found: List[Path] = []
    frontier: List[Tuple[List[str], List[RelationshipEdge]]] = [([from_id], [])]
    for _ in range(max_depth):
        next_frontier: List[Tuple[List[str], List[RelationshipEdge]]] = []
        for nodes, walked in frontier:
            for edge in sorted(outgoing.get(nodes[-1], []), key=lambda e: e.to_resource_id):
                if edge.to_resource_id in nodes:
                    continue
                candidate = (nodes + [edge.to_resource_id], walked + [edge])
                if edge.to_resource_id == to_id:
                    found.append(Path(nodes=candidate[0], edges=candidate[1]))
                    if len(found) >= limit:
                        return found
                else:
                    next_frontier.append(candidate)
        frontier = next_frontier
        if not frontier:
            break
    return found


def summarize_in_memory(nodes: Iterable[ResourceNode], edges: Iterable[RelationshipEdge], *, scan: Optional[Scan]) -> Summary:
    summary = Summary(scan=scan, nodes=0, edges=0)
    for node in nodes:
        summary.nodes += 1
        summary.by_type[node.type] = summary.by_type.get(node.type, 0) + 1
        summary.by_region[node.region] = summary.by_region.get(node.region, 0) + 1
        summary.by_account[node.account_id] = summary.by_account.get(node.account_id, 0) + 1
        summary.by_source[node.source or ""] = summary.by_source.get(node.source or "", 0) + 1
    for edge in edges:
        summary.edges += 1
        summary.edge_types[edge.relationship_type] = summary.edge_types.get(edge.relationship_type, 0) + 1
    return summary


# ── the registry ─────────────────────────────────────────────────────────────

StoreFactory = Callable[[Mapping[str, Any], str, Any], GraphStore]
_factory: Optional[StoreFactory] = None


def set_graph_store_factory(factory: Optional[StoreFactory]) -> None:
    """A larger deployment's store: called as ``factory(config, workspace_id, session)``.
    ``None`` restores the open default."""
    global _factory
    _factory = factory


def backend_name(config: Mapping[str, Any]) -> str:
    """Which store the workspace uses: ``COMPASS_MAP_STORE`` for this process, else the
    config's ``map.store``, else DynamoDB."""
    override = (os.getenv("COMPASS_MAP_STORE") or "").strip().lower()
    if override:
        return override
    section = config.get("map") if isinstance(config, Mapping) else None
    chosen = (section or {}).get("store") if isinstance(section, Mapping) else None
    return str(chosen or "dynamodb").strip().lower()


def _default_factory(config: Mapping[str, Any], workspace_id: str, session: Any) -> GraphStore:
    backend = backend_name(config)
    if backend == "dynamodb":
        from compass_core.tables import tables
        from infra_map.dynamo_store import DynamoGraphStore

        table_name = tables(config).map
        if not table_name:
            raise LookupError("dynamodb_config.map is not configured: the workspace config names no map table")
        return DynamoGraphStore(table_name=table_name, workspace_id=workspace_id, session=session)
    raise LookupError(f"map.store {backend!r} is not a store this Compass knows (dynamodb)")


def open_store(config: Mapping[str, Any], *, workspace_id: str, session: Any = None) -> GraphStore:
    """The workspace's map store. ``config`` is the workspace config (or just its
    ``dynamodb_config`` block); ``session`` is the control boto3 session when the caller
    has one."""
    factory = _factory or _default_factory
    return factory(config, workspace_id, session)
