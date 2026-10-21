"""What every map store promises (docs/specs/map-store.md section 3), run against each
backend: a scan's writes, what the next scan closes, point and paged reads with
cursors, neighbourhoods and their bound, paths, the summary. A backend without
history says so instead of guessing."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from infra_map.graph_store import BadCursor, EdgeQuery, NodeQuery
from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.synthetic import WorldSpec, generate

ORG = "acme"
OBSERVED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _node(rid: str, ntype: str = "lambda_function", *, region: str = "us-east-1", name: str = "", source: str = "aws_scan", scope: str = "123", **attrs) -> ResourceNode:
    return ResourceNode(org_id=ORG, resource_id=rid, type=ntype, account_id="123456789012", region=region, name=name or rid, attributes=attrs,
                        source=source, scope=scope, last_seen=OBSERVED)


def _edge(src: str, dst: str, rel: str, *, source: str = "aws_scan", scope: str = "123") -> RelationshipEdge:
    return RelationshipEdge(org_id=ORG, from_resource_id=src, to_resource_id=dst, relationship_type=rel, source=source, scope=scope, last_seen=OBSERVED)


@pytest.fixture(params=["sqlite", "dynamodb", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        from infra_map.sql_store import SqliteGraphStore

        s = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
        yield s
        s.close()
        return
    if request.param == "postgres":
        # never the network in CI: only with a database someone pointed us at
        dsn = os.getenv("COMPASS_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("COMPASS_TEST_POSTGRES_DSN is not set")
        from infra_map.sql_store import PostgresGraphStore

        s = PostgresGraphStore(dsn, f"ws-{uuid.uuid4().hex[:8]}")
        yield s
        with s.conn().raw.cursor() as cur:
            for table in ("nodes", "edges", "scans"):
                cur.execute(f"DELETE FROM {table} WHERE workspace_id = %s", (s.workspace_id,))
        s.close()
        return
    if request.param == "dynamodb":
        with mock_aws():
            from compass_core.commands import create_tables
            from infra_map.dynamo_store import DynamoGraphStore

            session = boto3.Session(region_name="us-east-1")
            create_tables({"dynamodb_config": {"map": "compass_map_t"}}, client=session.client("dynamodb"))
            yield DynamoGraphStore(table_name="compass_map_t", workspace_id="ws", session=session)
        return
    raise LookupError(request.param)


def _write(store, nodes, edges, **kw):
    with store.begin_scan(ORG, **kw) as scan:
        scan.put_nodes(nodes)
        scan.put_edges(edges)
    return scan.scan


def test_a_scan_writes_nodes_and_edges_and_the_next_scan_closes_what_it_did_not_see(store):
    a, b, c = _node("a"), _node("b", "iam_role"), _node("c", "s3_bucket")
    first = _write(store, [a, b, c], [_edge("a", "b", "uses_in"), _edge("a", "c", "depends_on")])
    assert first.status == "completed" and first.node_count == 3 and first.edge_count == 2 and first.added == 5
    assert store.get_node(ORG, "a").type == "lambda_function"
    assert set(store.get_nodes(ORG, ["a", "c", "nope"])) == {"a", "c"}

    b.attributes["changed"] = True
    second = _write(store, [a, b], [_edge("a", "b", "uses_in")])
    assert second.removed == 2  # c and a->c
    assert store.get_node(ORG, "c") is None
    assert store.get_node(ORG, "b").attributes == {"changed": True}
    assert {e.canonical_edge_id() for e in store.edges_of(ORG, "a")} == {_edge("a", "b", "uses_in").canonical_edge_id()}


def test_a_scoped_scan_leaves_other_scopes_alone(store):
    _write(store, [_node("a"), _node("r", "github_repo", source="github", scope="repo:x")], [])
    _write(store, [_node("a2")], [], scopes=[("aws_scan", "123")])
    assert store.get_node(ORG, "a") is None
    assert store.get_node(ORG, "a2") is not None
    assert store.get_node(ORG, "r") is not None


def test_an_aborted_scan_changes_nothing(store):
    _write(store, [_node("a")], [])
    with pytest.raises(RuntimeError):
        with store.begin_scan(ORG) as scan:
            scan.put_nodes([_node("b")])
            raise RuntimeError("collector died")
    assert store.get_node(ORG, "a") is not None and store.get_node(ORG, "b") is None


def test_nodes_page_with_a_cursor_that_belongs_to_the_query(store):
    nodes = [_node(f"n{i:03d}", "lambda_function" if i % 2 else "s3_bucket", region="eu-west-1" if i % 3 else "us-east-1", name=f"fn-{i:03d}") for i in range(23)]
    _write(store, nodes, [])
    query = NodeQuery(types=["lambda_function"])
    first = store.query_nodes(ORG, query, limit=5)
    assert len(first.items) == 5 and first.total == 11 and first.next_cursor
    assert [n.name for n in first.items] == sorted(n.name for n in first.items)
    seen = [n.resource_id for n in first.items]
    cursor = first.next_cursor
    while cursor:
        page = store.query_nodes(ORG, query, cursor=cursor, limit=5)
        assert page.total is None
        seen += [n.resource_id for n in page.items]
        cursor = page.next_cursor
    assert len(seen) == 11 and len(set(seen)) == 11 and all(store.get_node(ORG, rid).type == "lambda_function" for rid in seen)
    with pytest.raises(BadCursor):
        store.query_nodes(ORG, NodeQuery(types=["s3_bucket"]), cursor=first.next_cursor)
    with pytest.raises(BadCursor):
        store.query_nodes(ORG, query, cursor="not-a-cursor")
    assert store.query_nodes(ORG, NodeQuery(regions=["us-east-1"], text="FN-0")).total == len([n for n in nodes if n.region == "us-east-1" and "fn-0" in n.name])
    assert store.query_nodes(ORG, NodeQuery(ids=["n001", "n002"])).total == 2


def test_edges_page_and_filter_by_type_and_endpoint(store):
    nodes = [_node("a"), _node("b", "iam_role"), _node("c", "s3_bucket"), _node("d", "sqs_queue")]
    edges = [_edge("a", "b", "uses_in"), _edge("a", "c", "depends_on"), _edge("a", "d", "depends_on"), _edge("d", "c", "routes_to")]
    _write(store, nodes, edges)
    page = store.query_edges(ORG, EdgeQuery(types=["depends_on"]), limit=1)
    assert page.total == 2 and len(page.items) == 1 and page.next_cursor
    rest = store.query_edges(ORG, EdgeQuery(types=["depends_on"]), cursor=page.next_cursor, limit=1)
    assert rest.next_cursor is None and {page.items[0].to_resource_id, rest.items[0].to_resource_id} == {"c", "d"}
    assert store.query_edges(ORG, EdgeQuery(to_ids=["c"])).total == 2
    assert [e.relationship_type for e in store.edges_of(ORG, "c", direction="incoming")] == ["depends_on", "routes_to"] or \
        {e.relationship_type for e in store.edges_of(ORG, "c", direction="incoming")} == {"depends_on", "routes_to"}
    assert store.edges_of(ORG, "c", direction="outgoing") == []


def test_a_neighbourhood_is_bounded_and_says_when_it_was_cut(store):
    world = generate(WorldSpec(resources=120, regions=2, org_id=ORG, seed=5))
    _write(store, world.nodes, world.edges)
    hub = world.hubs[0]
    direct = {e.from_resource_id for e in world.edges if e.to_resource_id == hub} | {e.to_resource_id for e in world.edges if e.from_resource_id == hub}
    one = store.neighborhood(ORG, hub, depth=1, max_nodes=2000)
    assert {n.resource_id for n in one.nodes} == direct | {hub} and not one.truncated
    assert all(hub in (e.from_resource_id, e.to_resource_id) for e in one.edges)
    cut = store.neighborhood(ORG, hub, depth=2, max_nodes=10)
    assert len(cut.nodes) <= 10 and cut.truncated and any(n.resource_id == hub for n in cut.nodes)
    ids = {n.resource_id for n in cut.nodes}
    assert all(e.from_resource_id in ids and e.to_resource_id in ids for e in cut.edges)
    induced = store.neighborhood(ORG, hub, depth=1, max_nodes=2000, induced=True)
    assert len(induced.edges) >= len(one.edges)
    typed = store.neighborhood(ORG, hub, depth=1, types=["uses_in"], max_nodes=2000)
    assert all(e.relationship_type == "uses_in" for e in typed.edges)


def test_paths_are_shortest_first_over_the_asked_edge_types(store):
    nodes = [_node(x, "lambda_function") for x in "abcde"]
    edges = [_edge("a", "b", "depends_on"), _edge("b", "e", "depends_on"), _edge("a", "c", "depends_on"), _edge("c", "d", "depends_on"),
             _edge("d", "e", "depends_on"), _edge("a", "e", "uses_in")]
    _write(store, nodes, edges)
    found = store.paths(ORG, "a", "e", max_depth=4, limit=5)
    assert [p.nodes for p in found] == [["a", "e"], ["a", "b", "e"], ["a", "c", "d", "e"]]
    assert [p.nodes for p in store.paths(ORG, "a", "e", types=["depends_on"], limit=1)] == [["a", "b", "e"]]
    assert store.paths(ORG, "a", "e", max_depth=1, types=["depends_on"]) == []
    assert store.paths(ORG, "e", "a") == []


def test_the_summary_counts_nodes_by_type_region_and_source_and_edges_by_type(store):
    world = generate(WorldSpec(resources=80, regions=2, org_id=ORG, seed=5))
    _write(store, world.nodes, world.edges)
    summary = store.summary(ORG)
    assert summary.nodes == world.node_count and summary.edges == world.edge_count
    assert summary.by_type == world.by_type()
    assert summary.by_source["aws_scan"] == sum(1 for n in world.nodes if n.source == "aws_scan") and summary.by_region["global"] > 0
    assert sum(summary.edge_types.values()) == world.edge_count
    assert store.fetch_graph(ORG, limit=10)["nodes"] or store.fetch_graph(ORG, limit=10)["edges"]


def test_history_is_either_there_or_said_not_to_be(store):
    _write(store, [_node("a")], [])
    scans = store.list_scans(ORG)
    if not scans:
        with pytest.raises(NotImplementedError):
            store.diff(ORG, 1, 2)
        return
    assert scans[0].status == "completed" and scans[0].node_count == 1
