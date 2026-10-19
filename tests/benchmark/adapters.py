"""The benchmark seen through the GraphStore interface: one adapter for every backend,
so the numbers compare stores, not harness code. The reads are what the dashboard
contract (docs/specs/map-store.md section 8) and the resolvers ask for."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional

from infra_map.graph_store import EdgeQuery, GraphStore, NodeQuery
from tests.benchmark.world import World

DRAW_ALL_BELOW = 3000


class GraphStoreAdapter:
    def __init__(self, name: str, store: GraphStore, *, org_id: str = "bench", reset=None, size=None):
        self.name = name
        self.store = store
        self.org_id = org_id
        self._reset = reset
        self._size = size

    def reset(self) -> None:
        if self._reset:
            self._reset()

    def scan(self, world: World) -> None:
        with self.store.begin_scan(self.org_id, job_id="bench") as scan:
            scan.put_nodes(world.nodes)
            scan.put_edges(world.edges)

    def fetch_graph(self) -> Dict[str, Any]:
        return self.store.fetch_graph(self.org_id)

    def first_load(self) -> int:
        """The summary; under the draw-whole threshold, every node and edge in pages."""
        from compass_api.routes.maps import _serialize_edge, _serialize_node

        summary = self.store.summary(self.org_id)
        size = len(json.dumps(summary.as_dict(), default=str))
        if summary.nodes <= DRAW_ALL_BELOW:
            cursor = None
            while True:
                page = self.store.query_nodes(self.org_id, NodeQuery(), cursor=cursor, limit=5000)
                size += len(json.dumps([_serialize_node(n) for n in page.items], default=str))
                cursor = page.next_cursor
                if not cursor:
                    break
            cursor = None
            while True:
                page = self.store.query_edges(self.org_id, EdgeQuery(), cursor=cursor, limit=10000)
                size += len(json.dumps([_serialize_edge(e) for e in page.items], default=str))
                cursor = page.next_cursor
                if not cursor:
                    break
        return size

    def neighborhood(self, node_id: str, *, depth: int, max_nodes: int) -> int:
        return len(self.store.neighborhood(self.org_id, node_id, depth=depth, max_nodes=max_nodes, induced=True).nodes)

    def page(self, node_type: str, *, limit: int) -> int:
        return len(self.store.query_nodes(self.org_id, NodeQuery(types=[node_type]), limit=limit).items)

    def summary(self) -> Dict[str, Any]:
        summary = self.store.summary(self.org_id)
        return {"total": summary.nodes, **summary.as_dict()}

    def ownership(self, node_id: str) -> Dict[str, Any]:
        # the resolver as it is today: a whole-graph load, whatever the store (section 14, PR 5 moves it to paths)
        from captains.mapper.ownership_resolver import InfraMapOwnershipResolver

        return InfraMapOwnershipResolver(self.store).resolve_ownership(self.org_id, node_id)

    def paths(self, from_id: str, to_id: str) -> Optional[int]:
        return len(self.store.paths(self.org_id, from_id, to_id, max_depth=4, limit=5))

    def size_mb(self) -> Optional[float]:
        if self._size:
            value = self._size()
            return round(value / 1e6, 1) if value else None
        return None

    def close(self) -> None:
        self.store.close()


def resolve(name: str) -> GraphStoreAdapter:
    if name == "sqlite":
        from infra_map.sql_store import SqliteGraphStore

        path = os.getenv("BENCH_SQLITE_PATH") or os.path.join(os.getenv("TMPDIR", "/tmp"), f"compass-bench-{uuid.uuid4().hex[:8]}.sqlite")

        def reset():
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass

        reset()
        store = SqliteGraphStore(path, "bench")
        return GraphStoreAdapter("sqlite", store, reset=lambda: None, size=store.size_bytes)
    if name == "postgres":
        from infra_map.sql_store import PostgresGraphStore

        dsn = os.getenv("BENCH_POSTGRES_DSN", "postgresql://bench:bench@localhost:15432/bench")
        store = PostgresGraphStore(dsn, "bench")

        def reset():
            conn = store.conn()
            for table in ("nodes", "edges", "scans"):
                conn.execute(f"TRUNCATE {table}")
            conn.execute("VACUUM ANALYZE")

        return GraphStoreAdapter("postgres", store, reset=reset, size=store.size_bytes)
    if name == "dynamodb":
        import boto3

        from compass_core.commands import create_tables
        from infra_map.dynamo_store import DynamoGraphStore

        endpoint = os.getenv("BENCH_DYNAMODB_URL", "http://localhost:18000")
        table_name = "compass_map_bench_gs"
        session = boto3.Session(aws_access_key_id="bench", aws_secret_access_key="bench", region_name="us-east-1")
        client = session.client("dynamodb", endpoint_url=endpoint)
        table = session.resource("dynamodb", endpoint_url=endpoint).Table(table_name)

        def reset():
            if table_name in client.list_tables()["TableNames"]:
                client.delete_table(TableName=table_name)
                client.get_waiter("table_not_exists").wait(TableName=table_name)
            create_tables({"dynamodb_config": {"map": table_name}}, client=client)

        store = DynamoGraphStore(table_name=table_name, workspace_id="bench", session=session, table=table)
        return GraphStoreAdapter("dynamodb (GraphStore)", store, reset=reset)
    raise LookupError(f"no benchmark store named {name!r} (legacy, dynamodb, sqlite, postgres)")
