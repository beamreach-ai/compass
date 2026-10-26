"""The measurements, and the adapter that runs them against today's map code.

An adapter is the store seen from the benchmark: ``scan(world)`` is what the
mapper does after collection, the reads are what the dashboard, the IaC tab and
the resolvers ask. ``LegacyDynamoAdapter`` runs the code on ``main`` today
(``infra_map.store.InfraMapStore`` with the mapper's write block); the
GraphStore adapters live next to it once the interface exists.
"""

from __future__ import annotations

import gc
import json
import os
import resource
import sys
import time
from typing import Any, Callable, Dict, Optional, Protocol

from infra_map.synthetic import World


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


class Adapter(Protocol):
    name: str

    def reset(self) -> None: ...
    def scan(self, world: World) -> None: ...
    def fetch_graph(self) -> Dict[str, Any]: ...
    def first_load(self) -> int: ...                                  # bytes the dashboard's first request carries
    def neighborhood(self, node_id: str, *, depth: int, max_nodes: int) -> int: ...   # nodes returned
    def page(self, node_type: str, *, limit: int) -> int: ...
    def summary(self) -> Dict[str, Any]: ...
    def ownership(self, node_id: str) -> Dict[str, Any]: ...
    def paths(self, from_id: str, to_id: str) -> Optional[int]: ...  # None when the store cannot
    def size_mb(self) -> Optional[float]: ...
    def close(self) -> None: ...


def _timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    # the clock starts after a full collection: this process holds two worlds as dataclasses and
    # a pass over them takes 100 to 400 ms, which fell inside a single 40 ms read on CI
    gc.collect()
    start = time.perf_counter()
    out = fn()
    return time.perf_counter() - start, out


def measure(adapter: Adapter, world: World, churned: World, *, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Every metric of the spec for one adapter, in the order a day goes: scan, the dashboard
    opens, people ask questions, the next scan."""
    out: Dict[str, Any] = {"store": adapter.name, "nodes": world.node_count, "edges": world.edge_count}
    hub, typical = world.hubs[0], world.typical[0]
    managed = next(m for m in world.managed if m in churned.managed)
    lambda_id = world.typical[1]
    bucket_id = world.typical[2]

    adapter.reset()
    log(f"[{adapter.name}] first scan: {world.node_count} nodes, {world.edge_count} edges")
    out["scan_first_s"], _ = _timed(lambda: adapter.scan(world))
    log(f"[{adapter.name}] scan_first_s={out['scan_first_s']:.1f}")

    out["fetch_graph_s"], graph = _timed(adapter.fetch_graph)
    out["fetch_graph_nodes"] = len(graph.get("nodes", []))
    out["first_load_s"], out["first_load_bytes"] = _timed(adapter.first_load)
    seconds, n = _timed(lambda: adapter.neighborhood(hub, depth=2, max_nodes=500))
    out["neighborhood_hub_ms"], out["neighborhood_hub_nodes"] = seconds * 1000, n
    seconds, n = _timed(lambda: adapter.neighborhood(typical, depth=2, max_nodes=500))
    out["neighborhood_ms"], out["neighborhood_nodes"] = seconds * 1000, n
    seconds, n = _timed(lambda: adapter.page("lambda_function", limit=500))
    out["page_ms"], out["page_nodes"] = seconds * 1000, n
    seconds, summary = _timed(adapter.summary)
    out["summary_ms"], out["summary_total"] = seconds * 1000, summary.get("total")
    seconds, owner = _timed(lambda: adapter.ownership(managed))
    out["ownership_ms"], out["ownership_status"] = seconds * 1000, owner.get("status")
    seconds, found = _timed(lambda: adapter.paths(lambda_id, bucket_id))
    out["paths_ms"] = None if found is None else seconds * 1000
    log(f"[{adapter.name}] reads done: first_load={out['first_load_s']:.2f}s hub={out['neighborhood_hub_ms']:.0f}ms summary={out['summary_ms']:.0f}ms ownership={out['ownership_ms']:.0f}ms")

    log(f"[{adapter.name}] second scan (churn): {churned.node_count} nodes, {churned.edge_count} edges")
    out["scan_second_s"], _ = _timed(lambda: adapter.scan(churned))
    log(f"[{adapter.name}] scan_second_s={out['scan_second_s']:.1f}")
    out["db_size_mb"] = adapter.size_mb()
    out["peak_rss_mb"] = peak_rss_mb()
    adapter.close()
    return out


# ── today's code ─────────────────────────────────────────────────────────────

class LegacyDynamoAdapter:
    """The map as `main` writes and reads it: InfraMapStore on DynamoDB, the mapper's
    write block (captains/mapper/aws_mapper.py, "Writing infra map graph"), and the
    readers as the API calls them."""

    name = "dynamodb (today)"

    def __init__(self, *, endpoint_url: str, workspace_id: str = "bench", org_id: str = "bench", table_name: str = "compass_map_bench"):
        import boto3

        from infra_map.store import InfraMapStore

        self.org_id = org_id
        self.table_name = table_name
        self.workspace_id = workspace_id
        self.endpoint_url = endpoint_url
        self.session = boto3.Session(aws_access_key_id="bench", aws_secret_access_key="bench", region_name="us-east-1")
        self.client = self.session.client("dynamodb", endpoint_url=endpoint_url)
        table = self.session.resource("dynamodb", endpoint_url=endpoint_url).Table(table_name)
        self.store = InfraMapStore(table_name=table_name, workspace_id=workspace_id, session=self.session, table=table)
        self.items_written = 0

    def reset(self) -> None:
        from compass_core.commands import create_tables

        names = self.client.list_tables()["TableNames"]
        if self.table_name in names:
            self.client.delete_table(TableName=self.table_name)
            self.client.get_waiter("table_not_exists").wait(TableName=self.table_name)
        create_tables({"dynamodb_config": {"map": self.table_name}}, client=self.client)

    def scan(self, world: World) -> None:
        # captains/mapper/aws_mapper.py lines 198-215 on main, verbatim in shape
        store = self.store
        existing_graph = store.fetch_graph(self.org_id)
        existing_node_ids = {node.resource_id for node in existing_graph.get("nodes", [])}
        existing_edges = {edge.canonical_edge_id(): edge for edge in existing_graph.get("edges", [])}
        for node in world.nodes:
            store.put_resource(node)
        for edge in world.edges:
            store.put_relationship(edge)
        new_node_ids = {node.resource_id for node in world.nodes}
        new_edge_ids = {edge.canonical_edge_id() for edge in world.edges}
        for resource_id in existing_node_ids - new_node_ids:
            store.delete_resource(self.org_id, resource_id)
        for edge_id in set(existing_edges) - new_edge_ids:
            store.delete_relationship(existing_edges[edge_id])
        self.items_written += len(world.nodes) + len(world.edges)

    def fetch_graph(self) -> Dict[str, Any]:
        return self.store.fetch_graph(self.org_id)

    def first_load(self) -> int:
        # GET /api/maps?limit=4000, serialised as compass_api.routes.maps does
        from compass_api.routes.maps import _serialize_edge, _serialize_node

        graph = self.store.fetch_graph(self.org_id, limit=4000)
        body = {"nodes": [_serialize_node(n) for n in graph["nodes"]], "edges": [_serialize_edge(e) for e in graph["edges"]]}
        return len(json.dumps(body, default=str))

    def neighborhood(self, node_id: str, *, depth: int, max_nodes: int) -> int:
        return len(self.store.neighbors(self.org_id, node_id, depth=depth, direction="both")["nodes"])

    def page(self, node_type: str, *, limit: int) -> int:
        # there is no server-side filter today: the whole graph, filtered in Python
        graph = self.store.fetch_graph(self.org_id)
        return len([n for n in graph["nodes"] if n.type == node_type][:limit])

    def summary(self) -> Dict[str, Any]:
        from compass_api.routes.maps import _iac_coverage_summary

        return _iac_coverage_summary(self.org_id, self.store)

    def ownership(self, node_id: str) -> Dict[str, Any]:
        from captains.mapper.ownership_resolver import InfraMapOwnershipResolver

        return InfraMapOwnershipResolver(self.store).resolve_ownership(self.org_id, node_id)

    def paths(self, from_id: str, to_id: str) -> Optional[int]:
        return None

    def size_mb(self) -> Optional[float]:
        try:
            desc = self.client.describe_table(TableName=self.table_name)["Table"]
            return round(desc.get("TableSizeBytes", 0) / 1e6, 1) or None
        except Exception:  # noqa: BLE001 - DynamoDB-local reports 0 in memory mode
            return None

    def close(self) -> None:
        return None


def resolve_adapter(name: str) -> Adapter:
    """An adapter by its command-line name; the endpoints come from the environment."""
    if name in ("legacy", "dynamodb-legacy"):
        return LegacyDynamoAdapter(endpoint_url=os.getenv("BENCH_DYNAMODB_URL", "http://localhost:18000"))
    from tests.benchmark import adapters

    return adapters.resolve(name)
