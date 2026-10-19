"""The map in a SQL database with history: SQLite (the open default) and Postgres
(the same schema and queries through a second dialect), behind GraphStore.

Every node and edge row is a version with ``[valid_from, valid_to)``; the current
graph is the rows with ``valid_to IS NULL``, which partial indexes keep as cheap
as a table without history. A scan is one transaction: its rows land in a temp
table, then set operations mark what is unchanged as seen by this scan, close and
re-insert what changed, insert what is new, and at commit close what the scan
did not see within its scopes. Readers see the previous graph until commit.
docs/specs/map-store.md sections 3 to 6 are the design; the queries here are
the ones that document shows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
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
    Scan,
    ScanInProgress,
    Subgraph,
    Summary,
    decode_cursor,
    encode_cursor,
    query_key,
)
from infra_map.graph_store import (
    Path as GraphPath,
)
from infra_map.models import RelationshipEdge, ResourceNode, TerraformMetadata

logger = logging.getLogger("infra_map.sql_store")

SCHEMA_VERSION = 1
BATCH = 1000
STALE_SCAN_MS = 2 * 60 * 60 * 1000  # a running scan older than this was abandoned
IN_CHUNK = 500                      # ids per IN (...) list

NODE_COLUMNS = ("workspace_id", "org_id", "node_id", "type", "name", "provider", "account", "region", "source", "scope",
                "node_class", "confidence", "props", "content_hash", "first_seen", "last_seen", "valid_from", "valid_to", "seen_scan")
EDGE_COLUMNS = ("workspace_id", "org_id", "edge_id", "from_id", "to_id", "type", "source", "scope", "confidence", "props",
                "content_hash", "first_seen", "last_seen", "valid_from", "valid_to", "seen_scan")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: Optional[int]) -> datetime:
    return datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc)


def _json_default(value: Any) -> Any:
    """What json cannot encode on its own. DynamoDB hands numbers back as Decimal: they stay
    numbers (a migrated coverage_score of 1 must not become "1"); anything else is its string."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)


def _dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), **kwargs)


def _digest(payload: Any) -> str:
    return hashlib.sha1(_dumps(payload, sort_keys=True).encode()).hexdigest()


def provider_of(node_id: str) -> str:
    if node_id.startswith("arn:"):
        return "aws"
    if node_id.startswith("github://"):
        return "github"
    if node_id.startswith(("tfroot://", "tfstate://", "tfaddr://", "tfstate-instance://")):
        return "terraform"
    return ""


# ── rows ↔ models ────────────────────────────────────────────────────────────

def node_row(node: ResourceNode, *, workspace_id: str) -> Tuple:
    props: Dict[str, Any] = {"tags": node.tags or {}, "attributes": node.attributes or {}}
    if node.aliases:
        props["aliases"] = list(node.aliases)
    if node.canonical_ref:
        props["canonical_ref"] = node.canonical_ref
    if node.terraform:
        props["terraform"] = node.terraform.to_dict()
    if node.expires_at is not None:
        props["expires_at"] = node.expires_at
    content = {"type": node.type, "name": node.name, "account": node.account_id, "region": node.region, "source": node.source or "",
               "scope": node.scope or "", "node_class": node.node_class, "confidence": node.confidence, "props": props}
    seen = _ms(node.last_seen)
    return (workspace_id, node.org_id, node.resource_id, node.type, node.name, provider_of(node.resource_id), node.account_id, node.region,
            node.source or "", node.scope or "", node.node_class, node.confidence, _dumps(props),
            _digest(content), seen, seen, 0, None, 0)


def node_from_row(row: Sequence[Any]) -> ResourceNode:
    (_, org_id, node_id, ntype, name, _provider, account, region, source, scope, node_class, confidence, props, _hash,
     _first, last_seen, _from, _to) = row[:18]
    data = json.loads(props) if isinstance(props, str) else (props or {})
    return ResourceNode(
        org_id=org_id, resource_id=node_id, type=ntype, account_id=account, region=region, name=name,
        tags=data.get("tags", {}), attributes=data.get("attributes", {}), node_class=node_class,
        canonical_ref=data.get("canonical_ref"), aliases=data.get("aliases", []), confidence=confidence,
        source=source or None, scope=scope or None, last_seen=_dt(last_seen),
        terraform=TerraformMetadata.from_dict(data.get("terraform")), expires_at=data.get("expires_at"),
    )


def edge_row(edge: RelationshipEdge, *, workspace_id: str) -> Tuple:
    props: Dict[str, Any] = {"evidence": list(edge.evidence or []), "attributes": edge.attributes or {}}
    if edge.expires_at is not None:
        props["expires_at"] = edge.expires_at
    content = {"from": edge.from_resource_id, "to": edge.to_resource_id, "type": edge.relationship_type, "source": edge.source or "",
               "scope": edge.scope or "", "confidence": edge.confidence, "props": props}
    seen = _ms(edge.last_seen)
    return (workspace_id, edge.org_id, edge.canonical_edge_id(), edge.from_resource_id, edge.to_resource_id, edge.relationship_type,
            edge.source or "", edge.scope or "", edge.confidence, _dumps(props),
            _digest(content), seen, seen, 0, None, 0)


def edge_from_row(row: Sequence[Any]) -> RelationshipEdge:
    (_, org_id, edge_id, from_id, to_id, etype, source, scope, confidence, props, _hash, _first, last_seen, _from, _to) = row[:15]
    data = json.loads(props) if isinstance(props, str) else (props or {})
    return RelationshipEdge(
        org_id=org_id, from_resource_id=from_id, to_resource_id=to_id, relationship_type=etype, confidence=confidence,
        evidence=data.get("evidence", []), attributes=data.get("attributes", {}), source=source or None, scope=scope or None,
        last_seen=_dt(last_seen), edge_id=edge_id, expires_at=data.get("expires_at"),
    )


# ── dialects ─────────────────────────────────────────────────────────────────

class Dialect:
    """What differs between SQLite and Postgres in the statements below."""

    name = "sql"
    placeholder = "?"
    serial = "INTEGER PRIMARY KEY"
    json_type = "TEXT"
    like = "LIKE"
    temp = "CREATE TEMP TABLE"

    def q(self, sql: str) -> str:
        return sql

    def json_num(self, column: str, path: str) -> str:
        return f"CAST(json_extract({column}, '$.{path}') AS REAL)"

    def json_text(self, column: str, path: str) -> str:
        return f"json_extract({column}, '$.{path}')"

    def strpos(self, haystack: str, needle: str) -> str:
        return f"instr({haystack}, {needle})"

    def lower_like(self, column: str) -> str:
        return f"lower({column}) {self.like} ?"


class SqliteDialect(Dialect):
    name = "sqlite"


class PostgresDialect(Dialect):
    name = "postgres"
    placeholder = "%s"
    serial = "BIGSERIAL PRIMARY KEY"
    json_type = "JSONB"
    like = "LIKE"
    temp = "CREATE TEMP TABLE"

    def q(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def json_num(self, column: str, path: str) -> str:
        parts = path.split(".")
        inner = "".join(f"->'{p}'" for p in parts[:-1]) + f"->>'{parts[-1]}'"
        return f"NULLIF(({column}{inner}), '')::numeric"

    def json_text(self, column: str, path: str) -> str:
        parts = path.split(".")
        return column + "".join(f"->'{p}'" for p in parts[:-1]) + f"->>'{parts[-1]}'"

    def strpos(self, haystack: str, needle: str) -> str:
        return f"strpos({haystack}, {needle})"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
    scan_id {serial},
    workspace_id TEXT NOT NULL, org_id TEXT NOT NULL,
    started_at BIGINT NOT NULL, finished_at BIGINT,
    status TEXT NOT NULL, scopes TEXT, job_id TEXT,
    node_count INTEGER DEFAULT 0, edge_count INTEGER DEFAULT 0,
    added INTEGER DEFAULT 0, changed INTEGER DEFAULT 0, removed INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS scans_org ON scans (workspace_id, org_id, started_at);
CREATE TABLE IF NOT EXISTS nodes (
    row_id {serial},
    workspace_id TEXT NOT NULL, org_id TEXT NOT NULL, node_id TEXT NOT NULL,
    type TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    node_class TEXT, confidence TEXT NOT NULL DEFAULT 'high',
    props {json} NOT NULL, content_hash TEXT NOT NULL,
    first_seen BIGINT NOT NULL, last_seen BIGINT NOT NULL,
    valid_from BIGINT NOT NULL, valid_to BIGINT,
    seen_scan BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_current ON nodes (workspace_id, org_id, node_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS nodes_current_type ON nodes (workspace_id, org_id, type, name, row_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS nodes_current_region ON nodes (workspace_id, org_id, region) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS nodes_current_scope ON nodes (workspace_id, org_id, source, scope, seen_scan) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS nodes_history ON nodes (workspace_id, org_id, node_id, valid_from);
CREATE INDEX IF NOT EXISTS nodes_valid_from ON nodes (workspace_id, org_id, valid_from);
CREATE INDEX IF NOT EXISTS nodes_valid_to ON nodes (workspace_id, org_id, valid_to) WHERE valid_to IS NOT NULL;
CREATE TABLE IF NOT EXISTS edges (
    row_id {serial},
    workspace_id TEXT NOT NULL, org_id TEXT NOT NULL, edge_id TEXT NOT NULL,
    from_id TEXT NOT NULL, to_id TEXT NOT NULL, type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    props {json} NOT NULL, content_hash TEXT NOT NULL,
    first_seen BIGINT NOT NULL, last_seen BIGINT NOT NULL,
    valid_from BIGINT NOT NULL, valid_to BIGINT,
    seen_scan BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS edges_current ON edges (workspace_id, org_id, edge_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_current_from ON edges (workspace_id, org_id, from_id, type) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_current_to ON edges (workspace_id, org_id, to_id, type) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_current_type ON edges (workspace_id, org_id, type, from_id, to_id, row_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_current_scope ON edges (workspace_id, org_id, source, scope, seen_scan) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_history ON edges (workspace_id, org_id, edge_id, valid_from);
CREATE INDEX IF NOT EXISTS edges_valid_from ON edges (workspace_id, org_id, valid_from);
CREATE INDEX IF NOT EXISTS edges_valid_to ON edges (workspace_id, org_id, valid_to) WHERE valid_to IS NOT NULL;
"""


# ── the store ────────────────────────────────────────────────────────────────

class SqlGraphStore:
    """GraphStore over one SQL connection per thread; subclasses connect."""

    dialect: Dialect = Dialect()

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self._local = threading.local()
        self._conns: List[Any] = []
        self._lock = threading.Lock()

    # subclasses
    def _connect(self) -> Any:
        raise NotImplementedError

    def _begin_write(self, conn: Any) -> None:
        conn.execute("BEGIN")

    def _after_commit(self, conn: Any) -> None:
        return None

    def size_bytes(self) -> Optional[int]:
        return None

    # connections
    def conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            for conn in self._conns:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
            self._conns.clear()
        self._local = threading.local()

    def _ensure_schema(self, conn: Any) -> None:
        """Create the tables on first open. Opening an existing store only reads: a write
        here would wait behind a running scan in another process."""
        try:
            row = conn.execute(self.dialect.q("SELECT value FROM meta WHERE key = ?"), ("schema",)).fetchone()
            if row and int(row[0]) >= SCHEMA_VERSION:
                return
        except Exception:  # noqa: BLE001 - no meta table yet: a new store
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        script = SCHEMA.format(serial=self.dialect.serial, json=self.dialect.json_type)
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(self.dialect.q("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING"), ("schema", str(SCHEMA_VERSION)))
        conn.commit()

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> List[Sequence[Any]]:
        cur = self.conn().execute(self.dialect.q(sql), tuple(params))
        return cur.fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Sequence[Any]]:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # validity predicates
    @staticmethod
    def _valid(as_of: Optional[int], alias: str = "") -> Tuple[str, List[Any]]:
        p = f"{alias}." if alias else ""
        if as_of is None:
            return f"{p}valid_to IS NULL", []
        return f"{p}valid_from <= ? AND ({p}valid_to IS NULL OR {p}valid_to > ?)", [as_of, as_of]

    def _scope_clause(self, org_id: str, as_of: Optional[int], alias: str = "") -> Tuple[str, List[Any]]:
        p = f"{alias}." if alias else ""
        valid, params = self._valid(as_of, alias)
        return f"{p}workspace_id = ? AND {p}org_id = ? AND {valid}", [self.workspace_id, org_id, *params]

    # ── point reads ─────────────────────────────────────────────────────────
    def get_node(self, org_id: str, node_id: str, *, as_of: Optional[int] = None) -> Optional[ResourceNode]:
        where, params = self._scope_clause(org_id, as_of)
        row = self._one(f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes WHERE {where} AND node_id = ?", [*params, node_id])
        return node_from_row(row) if row else None

    def get_nodes(self, org_id: str, node_ids: Sequence[str], *, as_of: Optional[int] = None) -> Dict[str, ResourceNode]:
        out: Dict[str, ResourceNode] = {}
        ids = list(dict.fromkeys(node_ids))
        where, params = self._scope_clause(org_id, as_of)
        for i in range(0, len(ids), IN_CHUNK):
            chunk = ids[i : i + IN_CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in self._rows(f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes WHERE {where} AND node_id IN ({marks})", [*params, *chunk]):
                node = node_from_row(row)
                out[node.resource_id] = node
        return out

    # ── paged reads ─────────────────────────────────────────────────────────
    def _node_where(self, org_id: str, query: NodeQuery, as_of: Optional[int]) -> Tuple[str, List[Any]]:
        where, params = self._scope_clause(org_id, as_of)
        clauses = [where]
        for column, values in (("type", query.types), ("region", query.regions), ("account", query.accounts), ("source", query.sources), ("node_id", query.ids)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params += list(values)
        if query.text:
            needle = f"%{query.text.lower()}%"
            clauses.append(f"({self.dialect.lower_like('name')} OR {self.dialect.lower_like('node_id')})")
            params += [needle, needle]
        if query.coverage == "covered":
            clauses.append(f"{self.dialect.json_num('props', 'attributes.coverage_score')} >= 1")
        elif query.coverage == "unmanaged":
            clauses.append(f"COALESCE({self.dialect.json_num('props', 'attributes.coverage_score')}, 0) < 1")
        elif query.coverage == "drifted":
            clauses.append(f"{self.dialect.json_text('props', 'attributes.drift_status')} = 'drifted'")
        if query.changed_since is not None:
            clauses.append("valid_from > ?")
            params.append(query.changed_since)
        return " AND ".join(clauses), params

    def query_nodes(self, org_id: str, query: NodeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE,
                    as_of: Optional[int] = None) -> Page[ResourceNode]:
        key = query_key(query, org=org_id, kind="nodes", as_of=as_of)
        where, params = self._node_where(org_id, query, as_of)
        total = None
        if cursor:
            after = decode_cursor(cursor, query_key=key)["k"]
            where += " AND (type, name, row_id) > (?, ?, ?)"
            params += [after[0], after[1], int(after[2])]
        else:
            total = int(self._one(f"SELECT COUNT(*) FROM nodes WHERE {where}", params)[0])
        rows = self._rows(f"SELECT {', '.join(NODE_COLUMNS)}, row_id FROM nodes WHERE {where} ORDER BY type, name, row_id LIMIT ?", [*params, limit + 1])
        items = [node_from_row(r) for r in rows[:limit]]
        next_cursor = encode_cursor({"q": key, "k": [rows[limit - 1][3], rows[limit - 1][4], rows[limit - 1][-1]]}) if len(rows) > limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    def _edge_where(self, org_id: str, query: EdgeQuery, as_of: Optional[int]) -> Tuple[str, List[Any]]:
        where, params = self._scope_clause(org_id, as_of)
        clauses = [where]
        for column, values in (("type", query.types), ("from_id", query.from_ids), ("to_id", query.to_ids), ("source", query.sources)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params += list(values)
        return " AND ".join(clauses), params

    def query_edges(self, org_id: str, query: EdgeQuery, *, cursor: Optional[str] = None, limit: int = DEFAULT_EDGE_PAGE,
                    as_of: Optional[int] = None) -> Page[RelationshipEdge]:
        key = query_key(query, org=org_id, kind="edges", as_of=as_of)
        where, params = self._edge_where(org_id, query, as_of)
        total = None
        if cursor:
            after = decode_cursor(cursor, query_key=key)["k"]
            where += " AND (type, from_id, to_id, row_id) > (?, ?, ?, ?)"
            params += [after[0], after[1], after[2], int(after[3])]
        else:
            total = int(self._one(f"SELECT COUNT(*) FROM edges WHERE {where}", params)[0])
        rows = self._rows(f"SELECT {', '.join(EDGE_COLUMNS)}, row_id FROM edges WHERE {where} ORDER BY type, from_id, to_id, row_id LIMIT ?", [*params, limit + 1])
        items = [edge_from_row(r) for r in rows[:limit]]
        last = rows[limit - 1] if len(rows) > limit else None
        next_cursor = encode_cursor({"q": key, "k": [last[5], last[3], last[4], last[-1]]}) if last else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    # ── graph reads ─────────────────────────────────────────────────────────
    def _edges_touching(self, org_id: str, ids: Sequence[str], *, direction: Direction, types: Optional[Sequence[str]], limit: int,
                        as_of: Optional[int]) -> Tuple[List[RelationshipEdge], bool]:
        """Edges with an endpoint in ``ids``; (edges, hit_limit)."""
        out: List[RelationshipEdge] = []
        where, base = self._scope_clause(org_id, as_of)
        type_clause, type_params = "", []
        if types:
            type_clause = f" AND type IN ({','.join('?' * len(types))})"
            type_params = list(types)
        for i in range(0, len(ids), IN_CHUNK):
            chunk = list(ids[i : i + IN_CHUNK])
            marks = ",".join("?" * len(chunk))
            if direction == "outgoing":
                cond, params = f"from_id IN ({marks})", chunk
            elif direction == "incoming":
                cond, params = f"to_id IN ({marks})", chunk
            else:
                cond, params = f"(from_id IN ({marks}) OR to_id IN ({marks}))", chunk + chunk
            remaining = limit - len(out) + 1
            rows = self._rows(f"SELECT {', '.join(EDGE_COLUMNS)} FROM edges WHERE {where} AND {cond}{type_clause} ORDER BY type, from_id, to_id LIMIT ?",
                              [*base, *params, *type_params, remaining])
            out += [edge_from_row(r) for r in rows]
            if len(out) > limit:
                return out[:limit], True
        return out, False

    def edges_of(self, org_id: str, node_id: str, *, direction: Direction = "both", types: Optional[Sequence[str]] = None,
                 limit: int = MAX_EDGES_OF, as_of: Optional[int] = None) -> List[RelationshipEdge]:
        edges, _ = self._edges_touching(org_id, [node_id], direction=direction, types=types, limit=limit, as_of=as_of)
        return edges

    def neighborhood(self, org_id: str, node_id: str, *, depth: int = 1, direction: Direction = "both",
                     types: Optional[Sequence[str]] = None, max_nodes: int = DEFAULT_NEIGHBOURHOOD, induced: bool = False,
                     as_of: Optional[int] = None) -> Subgraph:
        """A breadth-first walk, one bounded query per level (the frontier is the IN list),
        so a hub with fifty thousand edges costs one capped query, not fifty thousand rows."""
        reached: Dict[str, int] = {node_id: 0}
        walked: Dict[str, RelationshipEdge] = {}
        frontier = [node_id]
        truncated = False
        for level in range(depth):
            if not frontier or len(reached) >= max_nodes:
                break
            edges, hit = self._edges_touching(org_id, frontier, direction=direction, types=types, limit=max_nodes * 8, as_of=as_of)
            truncated = truncated or hit
            at_frontier = set(frontier)
            next_frontier: List[str] = []
            for edge in edges:
                other = edge.to_resource_id if edge.from_resource_id in at_frontier else edge.from_resource_id
                if other not in reached:
                    if len(reached) >= max_nodes:
                        truncated = True
                        continue
                    reached[other] = level + 1
                    next_frontier.append(other)
                walked[edge.canonical_edge_id()] = edge
            frontier = next_frontier
        nodes = list(self.get_nodes(org_id, list(reached), as_of=as_of).values())
        ids = {n.resource_id for n in nodes}
        if induced:
            more, _ = self._edges_touching(org_id, list(ids), direction="outgoing", types=types, limit=max_nodes * 20, as_of=as_of)
            for edge in more:
                if edge.to_resource_id in ids:
                    walked.setdefault(edge.canonical_edge_id(), edge)
        edges = [e for e in walked.values() if e.from_resource_id in ids and e.to_resource_id in ids]
        return Subgraph(focus=node_id, nodes=nodes, edges=edges, truncated=truncated)

    def paths(self, org_id: str, from_id: str, to_id: str, *, max_depth: int = MAX_DEPTH, types: Optional[Sequence[str]] = None,
              limit: int = 5, as_of: Optional[int] = None) -> List[GraphPath]:
        where, params = self._scope_clause(org_id, as_of, alias="e")
        type_clause, type_params = "", []
        if types:
            type_clause = f" AND e.type IN ({','.join('?' * len(types))})"
            type_params = list(types)
        d = self.dialect
        sql = f"""
            WITH RECURSIVE walk(node_id, depth, path, edge_ids) AS (
                SELECT ?, 0, ?, ''
                UNION ALL
                SELECT e.to_id, w.depth + 1, w.path || e.to_id || '|', w.edge_ids || e.edge_id || '|'
                FROM walk w JOIN edges e ON e.from_id = w.node_id AND {where}{type_clause}
                WHERE w.depth < ? AND w.node_id <> ? AND {d.strpos('w.path', "'|' || e.to_id || '|'")} = 0
            )
            SELECT path, edge_ids, depth FROM walk WHERE node_id = ? ORDER BY depth, path LIMIT ?
        """
        rows = self._rows(sql, [from_id, f"|{from_id}|", *params, *type_params, max_depth, to_id, to_id, limit])
        out: List[GraphPath] = []
        for path, edge_ids, _depth in rows:
            ids = [p for p in path.split("|") if p]
            eids = [e for e in edge_ids.split("|") if e]
            found = self._edges_by_id(org_id, eids, as_of=as_of)
            out.append(GraphPath(nodes=ids, edges=[found[e] for e in eids if e in found]))
        return out

    def _edges_by_id(self, org_id: str, edge_ids: Sequence[str], *, as_of: Optional[int]) -> Dict[str, RelationshipEdge]:
        where, params = self._scope_clause(org_id, as_of)
        out: Dict[str, RelationshipEdge] = {}
        for i in range(0, len(edge_ids), IN_CHUNK):
            chunk = list(edge_ids[i : i + IN_CHUNK])
            for row in self._rows(f"SELECT {', '.join(EDGE_COLUMNS)} FROM edges WHERE {where} AND edge_id IN ({','.join('?' * len(chunk))})", [*params, *chunk]):
                edge = edge_from_row(row)
                out[edge.canonical_edge_id()] = edge
        return out

    def summary(self, org_id: str, *, as_of: Optional[int] = None) -> Summary:
        where, params = self._scope_clause(org_id, as_of)
        scans = self.list_scans(org_id, limit=1)
        summary = Summary(scan=scans[0] if scans else None, nodes=0, edges=0)
        for column, target in (("type", summary.by_type), ("region", summary.by_region), ("account", summary.by_account), ("source", summary.by_source)):
            for value, count in self._rows(f"SELECT {column}, COUNT(*) FROM nodes WHERE {where} GROUP BY {column} ORDER BY COUNT(*) DESC, {column}", params):
                target[value] = int(count)
        summary.nodes = sum(summary.by_type.values())
        for value, count in self._rows(f"SELECT type, COUNT(*) FROM edges WHERE {where} GROUP BY type ORDER BY COUNT(*) DESC, type", params):
            summary.edge_types[value] = int(count)
        summary.edges = sum(summary.edge_types.values())
        return summary

    # ── writes ──────────────────────────────────────────────────────────────
    def begin_scan(self, org_id: str, *, scopes: Optional[Sequence[Tuple[str, str]]] = None, job_id: Optional[str] = None) -> "_SqlScan":
        return _SqlScan(self, org_id, scopes=list(scopes) if scopes else None, job_id=job_id)

    # ── history ─────────────────────────────────────────────────────────────
    def list_scans(self, org_id: str, *, limit: int = 50) -> List[Scan]:
        rows = self._rows("SELECT scan_id, org_id, started_at, finished_at, status, scopes, job_id, node_count, edge_count, added, changed, removed, error "
                          "FROM scans WHERE workspace_id = ? AND org_id = ? AND status = 'completed' ORDER BY started_at DESC LIMIT ?",
                          [self.workspace_id, org_id, limit])
        return [self._scan_from_row(r) for r in rows]

    @staticmethod
    def _scan_from_row(row: Sequence[Any]) -> Scan:
        scopes = json.loads(row[5]) if row[5] else None
        return Scan(scan_id=int(row[0]), org_id=row[1], started_at=int(row[2]), finished_at=int(row[3]) if row[3] is not None else None, status=row[4],
                    scopes=[tuple(s) for s in scopes] if scopes else None, job_id=row[6], node_count=int(row[7] or 0), edge_count=int(row[8] or 0),
                    added=int(row[9] or 0), changed=int(row[10] or 0), removed=int(row[11] or 0), error=row[12])

    def get_scan(self, org_id: str, scan_id: int) -> Optional[Scan]:
        row = self._one("SELECT scan_id, org_id, started_at, finished_at, status, scopes, job_id, node_count, edge_count, added, changed, removed, error "
                        "FROM scans WHERE workspace_id = ? AND org_id = ? AND scan_id = ?", [self.workspace_id, org_id, scan_id])
        return self._scan_from_row(row) if row else None

    def diff(self, org_id: str, from_scan: int, to_scan: int, *, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE) -> Diff:
        """What is current at ``to_scan`` and was not at ``from_scan``, and the reverse,
        folded: a node seen in both with a different version is ``changed``."""
        a, b = self.get_scan(org_id, from_scan), self.get_scan(org_id, to_scan)
        if a is None or b is None:
            raise LookupError(f"no scan {from_scan if a is None else to_scan} for {org_id}")
        ta, tb = a.started_at, b.started_at
        key = query_key(None, org=org_id, kind="diff", a=from_scan, b=to_scan)
        items: List[Dict[str, Any]] = []
        for table, id_col, kind, columns, loader in (("nodes", "node_id", "node", NODE_COLUMNS, node_from_row), ("edges", "edge_id", "edge", EDGE_COLUMNS, edge_from_row)):
            at_a = "valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            at_b = at_a
            base = [self.workspace_id, org_id]
            sql = f"""
                SELECT x.{id_col}, ra.row_id, rb.row_id FROM (
                    SELECT {id_col} FROM {table} WHERE workspace_id = ? AND org_id = ? AND ({at_a} OR {at_b})
                ) x
                LEFT JOIN {table} ra ON ra.workspace_id = ? AND ra.org_id = ? AND ra.{id_col} = x.{id_col} AND {at_a.replace('valid', 'ra.valid')}
                LEFT JOIN {table} rb ON rb.workspace_id = ? AND rb.org_id = ? AND rb.{id_col} = x.{id_col} AND {at_b.replace('valid', 'rb.valid')}
                WHERE ra.row_id IS NULL OR rb.row_id IS NULL OR ra.row_id <> rb.row_id
                GROUP BY x.{id_col}, ra.row_id, rb.row_id ORDER BY x.{id_col}
            """
            for ident, row_a, row_b in self._rows(sql, [*base, ta, ta, tb, tb, *base, ta, ta, *base, tb, tb]):
                change = "added" if row_a is None else "removed" if row_b is None else "changed"
                before = self._row_by_id(table, columns, row_a) if row_a is not None else None
                after = self._row_by_id(table, columns, row_b) if row_b is not None else None
                item: Dict[str, Any] = {"kind": kind, "id": ident, "change": change,
                                        "before": loader(before) if before else None, "after": loader(after) if after else None}
                if change == "changed":
                    pa = json.loads(before[12 if kind == "node" else 9]) if isinstance(before[12 if kind == "node" else 9], str) else before[12 if kind == "node" else 9]
                    pb = json.loads(after[12 if kind == "node" else 9]) if isinstance(after[12 if kind == "node" else 9], str) else after[12 if kind == "node" else 9]
                    item["changed_keys"] = sorted(_changed_keys(pa, pb) | _changed_columns(before, after, columns))
                items.append(item)
        order = {"removed": 0, "added": 1, "changed": 2}
        items.sort(key=lambda i: (order[i["change"]], i["kind"], i["id"]))
        start = 0
        if cursor:
            start = int(decode_cursor(cursor, query_key=key)["o"])
        chunk = items[start : start + limit]
        next_cursor = encode_cursor({"q": key, "o": start + limit}) if start + limit < len(items) else None
        return Diff(from_scan=from_scan, to_scan=to_scan, items=chunk, next_cursor=next_cursor)

    def _row_by_id(self, table: str, columns: Sequence[str], row_id: int) -> Optional[Sequence[Any]]:
        return self._one(f"SELECT {', '.join(columns)} FROM {table} WHERE row_id = ?", [row_id])

    def prune(self, org_id: str, *, keep_days: int, keep_scans: int) -> int:
        """Delete closed versions older than ``keep_days`` and older than the last
        ``keep_scans`` scans, whichever keeps more; never a current row."""
        floor_by_days = int(time.time() * 1000) - keep_days * 86400 * 1000
        floor = floor_by_days
        if keep_scans > 0:
            scans = self.list_scans(org_id, limit=keep_scans)
            if len(scans) < keep_scans:
                return 0  # not enough scans yet to let any of them go
            floor = min(floor_by_days, scans[keep_scans - 1].started_at)  # rows closed at or after that scan stay: its diff still answers
        conn = self.conn()
        self._begin_write(conn)
        try:
            removed = 0
            for table in ("nodes", "edges"):
                cur = conn.execute(self.dialect.q(f"DELETE FROM {table} WHERE workspace_id = ? AND org_id = ? AND valid_to IS NOT NULL AND valid_to < ?"),
                                   (self.workspace_id, org_id, floor))
                removed += cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.execute(self.dialect.q("DELETE FROM scans WHERE workspace_id = ? AND org_id = ? AND started_at < ? AND status <> 'completed'"),
                         (self.workspace_id, org_id, floor))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return removed

    # ── compatibility ───────────────────────────────────────────────────────
    def fetch_graph(self, org_id: str, *, limit: Optional[int] = None) -> Dict[str, List]:
        where, params = self._scope_clause(org_id, None)
        nodes: List[ResourceNode] = []
        edges: List[RelationshipEdge] = []
        cap = f" LIMIT {int(limit)}" if limit else ""
        for row in self._rows(f"SELECT {', '.join(NODE_COLUMNS)} FROM nodes WHERE {where} ORDER BY row_id{cap}", params):
            nodes.append(node_from_row(row))
        remaining = None if limit is None else max(0, limit - len(nodes))
        if remaining != 0:
            cap = f" LIMIT {int(remaining)}" if remaining else ""
            for row in self._rows(f"SELECT {', '.join(EDGE_COLUMNS)} FROM edges WHERE {where} ORDER BY row_id{cap}", params):
                edges.append(edge_from_row(row))
        return {"nodes": nodes, "edges": edges}


def _changed_keys(a: Dict[str, Any], b: Dict[str, Any], prefix: str = "") -> set:
    out: set = set()
    for key in set(a) | set(b):
        va, vb = a.get(key), b.get(key)
        if isinstance(va, dict) and isinstance(vb, dict):
            out |= _changed_keys(va, vb, f"{prefix}{key}.")
        elif va != vb:
            out.add(f"{prefix}{key}")
    return out


def _changed_columns(a: Sequence[Any], b: Sequence[Any], columns: Sequence[str]) -> set:
    skip = {"props", "content_hash", "first_seen", "last_seen", "valid_from", "valid_to", "seen_scan", "row_id"}
    return {c for c, va, vb in zip(columns, a, b, strict=False) if c not in skip and va != vb}


class _SqlScan:
    """One scan, one transaction. Rows go to temp tables first; the set operations in
    ``commit`` are the whole write."""

    def __init__(self, store: SqlGraphStore, org_id: str, *, scopes: Optional[List[Tuple[str, str]]], job_id: Optional[str]):
        self.store = store
        self.org_id = org_id
        self.scopes = scopes
        self.conn = store.conn()
        self.d = store.dialect
        self.done = False
        now = int(time.time() * 1000)
        store._begin_write(self.conn)
        try:
            self._claim(now)
            self.scan = Scan(scan_id=0, org_id=org_id, started_at=now, scopes=scopes, job_id=job_id)
            cur = self.conn.execute(self.d.q("INSERT INTO scans (workspace_id, org_id, started_at, status, scopes, job_id) VALUES (?, ?, ?, 'running', ?, ?) RETURNING scan_id"),
                                    (store.workspace_id, org_id, now, json.dumps(scopes) if scopes else None, job_id))
            self.scan.scan_id = int(cur.fetchone()[0])
            self.conn.execute(f"{self.d.temp} incoming_nodes AS SELECT {', '.join(NODE_COLUMNS)} FROM nodes WHERE 1 = 0")
            self.conn.execute(f"{self.d.temp} incoming_edges AS SELECT {', '.join(EDGE_COLUMNS)} FROM edges WHERE 1 = 0")
        except Exception:
            self.conn.rollback()
            raise

    def _claim(self, now: int) -> None:
        row = self.store._one("SELECT scan_id, started_at FROM scans WHERE workspace_id = ? AND org_id = ? AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                              [self.store.workspace_id, self.org_id])
        if row and now - int(row[1]) < STALE_SCAN_MS:
            raise ScanInProgress(f"a scan of {self.org_id} started {int((now - int(row[1])) / 1000)}s ago is still being written")
        if row:
            self.conn.execute(self.d.q("UPDATE scans SET status = 'aborted', error = 'abandoned' WHERE scan_id = ?"), (row[0],))

    def put_nodes(self, nodes: Iterable[ResourceNode]) -> None:
        self._insert("incoming_nodes", NODE_COLUMNS, (node_row(n, workspace_id=self.store.workspace_id) for n in nodes))

    def put_edges(self, edges: Iterable[RelationshipEdge]) -> None:
        self._insert("incoming_edges", EDGE_COLUMNS, (edge_row(e, workspace_id=self.store.workspace_id) for e in edges))

    def _insert(self, table: str, columns: Sequence[str], rows: Iterable[Tuple]) -> None:
        if hasattr(self.conn, "copy_rows"):
            self.conn.copy_rows(table, columns, rows)
            return
        sql = self.d.q(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})")
        batch: List[Tuple] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= BATCH:
                self.conn.executemany(sql, batch)
                batch = []
        if batch:
            self.conn.executemany(sql, batch)

    def _apply(self, table: str, incoming: str, id_col: str, columns: Sequence[str], T: int) -> Tuple[int, int, int]:
        """unchanged: mark seen; changed: close and re-insert; new: insert. Returns (added, changed, unchanged).

        ``last_seen`` is when the collector observed the row (what the resolvers call freshness),
        carried from the incoming row; ``seen_scan`` is which scan saw it, which is what decides
        what the scan closes. A row copied from an old map keeps the time the old map recorded."""
        ws, org, sid = self.store.workspace_id, self.org_id, self.scan.scan_id
        q = self.d.q
        # the set operations below join the table against the temp table by id, thousands of times
        self.conn.execute(f"CREATE INDEX {incoming}_id ON {incoming} ({id_col})")
        self.conn.execute(f"ANALYZE {incoming}")  # without statistics Postgres nests loops over the temp table
        # the latest version per id in the temp table wins (a scan may put the same id twice)
        self.conn.execute(q(f"DELETE FROM {incoming} WHERE rowid NOT IN (SELECT MAX(rowid) FROM {incoming} GROUP BY {id_col})") if self.d.name == "sqlite"
                          else q(f"DELETE FROM {incoming} a USING {incoming} b WHERE a.{id_col} = b.{id_col} AND a.ctid < b.ctid"))
        cur = self.conn.execute(q(f"""
            UPDATE {table} SET seen_scan = ?,
                   last_seen = (SELECT i.last_seen FROM {incoming} i WHERE i.{id_col} = {table}.{id_col})
            WHERE workspace_id = ? AND org_id = ? AND valid_to IS NULL
              AND EXISTS (SELECT 1 FROM {incoming} i WHERE i.{id_col} = {table}.{id_col} AND i.content_hash = {table}.content_hash)"""), (sid, ws, org))
        unchanged = cur.rowcount
        cur = self.conn.execute(q(f"""
            UPDATE {table} SET valid_to = ? WHERE workspace_id = ? AND org_id = ? AND valid_to IS NULL
              AND EXISTS (SELECT 1 FROM {incoming} i WHERE i.{id_col} = {table}.{id_col} AND i.content_hash <> {table}.content_hash)"""), (T, ws, org))
        changed = cur.rowcount
        insert_cols = [c for c in columns if c not in ("first_seen", "last_seen", "valid_from", "valid_to", "seen_scan")]
        # What is current now (the changed versions were just closed, so they are not) goes into
        # its own temp table: joined from there, the insert never reads the table it is growing.
        # Postgres otherwise plans a nested loop over a table its statistics say is empty and
        # re-scans the growing heap once per row (quadratic: 75 minutes at 100k).
        current = f"{incoming}_current"
        self.conn.execute(q(f"{self.d.temp} {current} AS SELECT {id_col} FROM {table} WHERE workspace_id = ? AND org_id = ? AND valid_to IS NULL"), (ws, org))
        self.conn.execute(f"CREATE INDEX {current}_id ON {current} ({id_col})")
        self.conn.execute(f"ANALYZE {current}")
        # first_seen carries over from any earlier version (a resource that comes back keeps its
        # first sighting); the history is aggregated once, not looked up per row
        cur = self.conn.execute(q(f"""
            INSERT INTO {table} ({', '.join(insert_cols)}, first_seen, last_seen, valid_from, valid_to, seen_scan)
            SELECT {', '.join('i.' + c for c in insert_cols)}, COALESCE(h.first_seen, i.first_seen), i.last_seen, ?, NULL, ?
            FROM {incoming} i
            LEFT JOIN {current} c ON c.{id_col} = i.{id_col}
            LEFT JOIN (SELECT {id_col}, MIN(first_seen) AS first_seen FROM {table} WHERE workspace_id = ? AND org_id = ? GROUP BY {id_col}) h
                   ON h.{id_col} = i.{id_col}
            WHERE c.{id_col} IS NULL"""), (T, sid, ws, org))
        inserted = cur.rowcount
        self.conn.execute(f"DROP TABLE {current}")
        return inserted - changed, changed, unchanged

    def _close_unseen(self, table: str, T: int) -> int:
        ws, org = self.store.workspace_id, self.org_id
        sql = f"UPDATE {table} SET valid_to = ? WHERE workspace_id = ? AND org_id = ? AND valid_to IS NULL AND seen_scan <> ?"
        params: List[Any] = [T, ws, org, self.scan.scan_id]
        if self.scopes:
            sql += " AND (" + " OR ".join("(source = ? AND scope = ?)" for _ in self.scopes) + ")"
            for source, scope in self.scopes:
                params += [source, scope]
        return self.conn.execute(self.d.q(sql), tuple(params)).rowcount

    def commit(self) -> Scan:
        if self.done:
            return self.scan
        T = self.scan.started_at
        try:
            n_added, n_changed, _ = self._apply("nodes", "incoming_nodes", "node_id", NODE_COLUMNS, T)
            e_added, e_changed, _ = self._apply("edges", "incoming_edges", "edge_id", EDGE_COLUMNS, T)
            removed = self._close_unseen("nodes", T) + self._close_unseen("edges", T)
            counts = self.store._one("SELECT (SELECT COUNT(*) FROM incoming_nodes), (SELECT COUNT(*) FROM incoming_edges)")
            self.scan.node_count, self.scan.edge_count = int(counts[0]), int(counts[1])
            self.scan.added, self.scan.changed, self.scan.removed = n_added + e_added, n_changed + e_changed, removed
            self.scan.finished_at = int(time.time() * 1000)
            self.scan.status = "completed"
            self.conn.execute(self.d.q("UPDATE scans SET finished_at = ?, status = 'completed', node_count = ?, edge_count = ?, added = ?, changed = ?, removed = ? WHERE scan_id = ?"),
                              (self.scan.finished_at, self.scan.node_count, self.scan.edge_count, self.scan.added, self.scan.changed, self.scan.removed, self.scan.scan_id))
            self.conn.execute("DROP TABLE incoming_nodes")
            self.conn.execute("DROP TABLE incoming_edges")
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._record_failure(str(exc))
            raise
        self.done = True
        self.store._after_commit(self.conn)
        return self.scan

    def _record_failure(self, error: str) -> None:
        try:
            self.conn.execute(self.d.q("INSERT INTO scans (workspace_id, org_id, started_at, finished_at, status, scopes, job_id, error) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)"),
                              (self.store.workspace_id, self.org_id, self.scan.started_at, int(time.time() * 1000), json.dumps(self.scopes) if self.scopes else None,
                               self.scan.job_id, error[:2000]))
            self.conn.commit()
        except Exception:  # noqa: BLE001 - the failure is already being raised
            self.conn.rollback()

    def abort(self, error: Optional[str] = None) -> None:
        if self.done:
            return
        self.conn.rollback()
        self.scan.status = "aborted"
        self.scan.error = error
        self._record_failure(error or "aborted")
        self.done = True

    def __enter__(self) -> "_SqlScan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.abort(str(exc))


# ── SQLite ───────────────────────────────────────────────────────────────────

class SqliteGraphStore(SqlGraphStore):
    """One file per workspace, WAL, readers never blocked by the scan."""

    dialect = SqliteDialect()

    def __init__(self, path: str | os.PathLike, workspace_id: str, *, busy_timeout_ms: int = 10_000):
        super().__init__(workspace_id)
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.conn()
        self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000, isolation_level=None, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            logger.warning("the map file %s could not be put in WAL mode (%s): readers wait during a scan; keep it on a named volume", self.path, mode)
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        return conn

    def _begin_write(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def _after_commit(self, conn: sqlite3.Connection) -> None:
        # without statistics the planner reaches for the wrong index on the graph queries
        # (a path query at 100k went from 1.8 s to under a millisecond with them)
        conn.execute("ANALYZE")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:  # a reader holds the WAL; the next writer checkpoints
            pass

    def size_bytes(self) -> Optional[int]:
        total = 0
        for suffix in ("", "-wal"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def backup(self, out: str | os.PathLike) -> Path:
        """A consistent copy while the stack runs (VACUUM INTO)."""
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        self.conn().execute("VACUUM INTO ?", (str(out),))
        return out


# ── Postgres ─────────────────────────────────────────────────────────────────

class PostgresGraphStore(SqlGraphStore):
    """The same store on Postgres: ``pip install compass[postgres]`` (psycopg) and
    ``map: {store: postgres, dsn: ...}``. For a hosted, multi-tenant deployment; the
    open edition's default is the SQLite file."""

    dialect = PostgresDialect()

    def __init__(self, dsn: str, workspace_id: str):
        super().__init__(workspace_id)
        self.dsn = dsn
        self._ensure_schema(self.conn())

    def _connect(self) -> Any:
        import psycopg  # noqa: PLC0415 - optional dependency

        conn = psycopg.connect(self.dsn, autocommit=True)
        return _PgConn(conn)

    def _begin_write(self, conn: Any) -> None:
        conn.execute("BEGIN")

    def _after_commit(self, conn: Any) -> None:
        conn.execute("ANALYZE nodes")
        conn.execute("ANALYZE edges")

    def size_bytes(self) -> Optional[int]:
        row = self._one("SELECT pg_total_relation_size('nodes') + pg_total_relation_size('edges') + pg_total_relation_size('scans')")
        return int(row[0]) if row else None


class _PgConn:
    """psycopg's connection with the sqlite3 shape the store uses: execute returns a
    cursor with rowcount/fetch, executemany batches, commit/rollback end the block."""

    def __init__(self, conn: Any):
        self.raw = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self.raw.cursor()
        cur.execute(sql, tuple(params) if params else None)
        return cur

    def executemany(self, sql: str, rows: Sequence[Tuple]) -> None:
        with self.raw.cursor() as cur:
            cur.executemany(sql, rows)

    def copy_rows(self, table: str, columns: Sequence[str], rows: Iterable[Tuple]) -> None:
        with self.raw.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)

    def commit(self) -> None:
        self.raw.execute("COMMIT") if self.raw.info.transaction_status != 0 else None

    def rollback(self) -> None:
        if self.raw.info.transaction_status != 0:
            self.raw.execute("ROLLBACK")

    def close(self) -> None:
        self.raw.close()
