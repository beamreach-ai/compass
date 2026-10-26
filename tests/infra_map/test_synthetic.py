"""The synthetic world on a live map (demo mode): seeded under its own scope beside a real
scan, left alone by the mapper's scans, a no-op when seeded again, replaced when seeded
with other numbers, refused outside demo mode. On SQLite, and on Postgres when
COMPASS_TEST_POSTGRES_DSN points at one."""

from __future__ import annotations

import os
import uuid

import pytest

from captains.mapper.aws_mapper import scan_scopes
from compass_core import config as config_module
from compass_core import demo
from infra_map import synthetic
from infra_map.graph_store import open_store
from infra_map.models import RelationshipEdge, ResourceNode

ORG = "acme"
ACCOUNT = "123456789012"


@pytest.fixture(params=["sqlite", "postgres"])
def workspace(request, tmp_path, monkeypatch):
    """A workspace config on one store, loaded by id as the commands load it."""
    workspace_id = f"ws-{uuid.uuid4().hex[:8]}"
    config = {"workspace_id": workspace_id, "org_id": ORG, "aws": {"account_id": ACCOUNT},
              "dynamodb_config": {"workspace_id": workspace_id, "map": "compass_map", "items": "compass_items"}}
    if request.param == "sqlite":
        config["map"] = {"store": "sqlite", "path": str(tmp_path / "map.sqlite")}
    else:
        dsn = os.getenv("COMPASS_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("COMPASS_TEST_POSTGRES_DSN is not set")
        config["map"] = {"store": "postgres", "dsn": dsn}
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    monkeypatch.setattr(config_module, "_loader", lambda ws: dict(config) if ws == workspace_id else (_ for _ in ()).throw(LookupError(ws)))
    monkeypatch.setenv("APP_MODE", "demo")
    yield workspace_id, config
    store = open_store(config, workspace_id=workspace_id)
    if request.param == "postgres":
        with store.conn().raw.cursor() as cur:
            for table in ("nodes", "edges", "scans"):
                cur.execute(f"DELETE FROM {table} WHERE workspace_id = %s", (workspace_id,))
    store.close()


def _real_scan(config, workspace_id, names):
    """What the mapper writes for the real account: a few resources under its own scopes, closing
    what it no longer sees within them, through the same scope rule the mapper uses."""
    nodes = [ResourceNode(org_id=ORG, resource_id=f"arn:aws:s3:::{n}", type="s3_bucket", account_id=ACCOUNT, region="global", name=n,
                          attributes={"coverage_score": 0}, source="aws_scan", scope="aws:full") for n in names]
    nodes.append(ResourceNode(org_id=ORG, resource_id="github://repo/beamreach/demo", type="github_repo", account_id="acme", region="global", name="beamreach/demo",
                              node_class="artifact", source="git_scan", scope="repo:beamreach/demo"))
    edges = [RelationshipEdge(org_id=ORG, from_resource_id=nodes[0].resource_id, to_resource_id=nodes[-1].resource_id, relationship_type="part_of",
                              source="derived", scope="derived:global")]
    store = open_store(config, workspace_id=workspace_id)
    try:
        with store.begin_scan(ORG, scopes=scan_scopes(store, ORG, nodes, edges), job_id="mapper") as scan:
            scan.put_nodes(nodes)
            scan.put_edges(edges)
        return scan.scan, store.scopes(ORG)
    finally:
        store.close()


def _synthetic_rows(counts):
    return sum(c for (_, scope), c in counts.items() if synthetic.is_synthetic_scope(scope))


def test_the_world_is_seeded_beside_a_real_scan_and_a_rescan_leaves_it_alone(workspace):
    workspace_id, config = workspace
    _real_scan(config, workspace_id, ["acme-legacy-exports", "acme-logs"])

    first = demo.demo({"verb": "seed", "workspace_id": workspace_id, "resources": 300, "seed": 7})
    assert first["scope"] == "demo:seed:7" and first["scan"]["status"] == "completed"
    assert first["scan"]["added"] == first["nodes"] + first["edges"] and first["scan"]["removed"] == 0 and first["closed_scopes"] == []
    by_scope = {(r["source"], r["scope"]): r for r in first["map"]}
    assert by_scope[("aws_scan", "aws:full")]["rows"] == 2 and by_scope[("aws_scan", "aws:full")]["synthetic"] is False
    assert by_scope[("git_scan", "repo:beamreach/demo")]["rows"] == 1 and by_scope[("derived", "derived:global")]["rows"] == 1
    synthetic_pairs = {k for k, r in by_scope.items() if r["synthetic"]}
    assert synthetic_pairs == {("aws_scan", "demo:seed:7"), ("git_scan", "demo:seed:7"), ("tf_state", "demo:seed:7")}
    assert sum(by_scope[k]["rows"] for k in synthetic_pairs) == first["nodes"] + first["edges"]

    store = open_store(config, workspace_id=workspace_id)
    try:
        summary = store.summary(ORG)
        assert summary.nodes == first["nodes"] + 3
        # the synthetic resources count as resources: coverage, suggestions and the detail panel see aws_scan rows marked synthetic
        world = synthetic.generate(synthetic.WorldSpec(resources=300, seed=7, org_id=ORG, account=ACCOUNT, scope="demo:seed:7", synthetic=True))
        node = store.get_node(ORG, world.managed[0])
        assert node.source == "aws_scan" and node.scope == "demo:seed:7" and node.attributes["synthetic"] is True and node.attributes["coverage_score"] == 1
        assert store.get_node(ORG, "arn:aws:s3:::acme-logs").attributes.get("synthetic") is None
    finally:
        store.close()

    # the real account changes and is rescanned: its removed bucket is closed, the world is untouched
    rescan, counts = _real_scan(config, workspace_id, ["acme-legacy-exports"])
    assert rescan.removed == 1 and counts[("aws_scan", "aws:full")] == 1
    assert _synthetic_rows(counts) == first["nodes"] + first["edges"]

    # the same seed again: every row unchanged, nothing added or closed
    again = demo.demo({"verb": "seed", "workspace_id": workspace_id, "resources": 300, "seed": 7})
    assert (again["scan"]["added"], again["scan"]["changed"], again["scan"]["removed"]) == (0, 0, 0)
    assert again["closed_scopes"] == []

    # other numbers: the old world is closed row for row (the repositories keep their ids across seeds and
    # are rewritten under the new scope), the new one is there, the real account still is
    other = demo.demo({"verb": "seed", "workspace_id": workspace_id, "resources": 120, "seed": 8})
    assert other["scope"] == "demo:seed:8" and other["scan"]["removed"] + other["scan"]["changed"] == first["nodes"] + first["edges"]
    assert other["scan"]["changed"] <= 12, "only the repositories, and the Dockerfiles of the few services both worlds happen to name"
    assert other["closed_scopes"] == [{"source": s, "scope": "demo:seed:7"} for s in ("aws_scan", "git_scan", "tf_state")]
    remaining = {(r["source"], r["scope"]): r["rows"] for r in other["map"]}
    assert not any(scope == "demo:seed:7" for _, scope in remaining)
    assert remaining[("aws_scan", "aws:full")] == 1 and _synthetic_rows({k: v for k, v in remaining.items()}) == other["nodes"] + other["edges"]


def test_the_seed_takes_its_numbers_from_the_demo_block_and_refuses_outside_demo_mode(workspace, monkeypatch):
    workspace_id, config = workspace
    config["demo"] = {"world": {"resources": 150, "seed": 3, "edges": 900}}
    seen = {}
    demo.register_seeder("test", lambda cfg, ws, world: seen.update(ws=ws, nodes=world.node_count, edges=world.edge_count) or "ok")
    try:
        result = demo.demo({"verb": "seed", "workspace_id": workspace_id})
    finally:
        demo._seeders.pop("test", None)
    assert result["resources"] == 150 and result["seed"] == 3 and result["scope"] == "demo:seed:3"
    assert result["edges"] == 900, "the block's edges target densifies the world"
    assert result["seeded"] == {"test": "ok"} and seen == {"ws": workspace_id, "nodes": result["nodes"], "edges": 900}, "a registered seeder gets the world"
    assert demo.demo({"verb": "seed", "workspace_id": workspace_id, "edges": 0})["edges"] < 900, "edges=0 is the rules alone"

    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(ValueError, match="runs only in demo mode"):
        demo.demo({"verb": "seed", "workspace_id": workspace_id})
    with pytest.raises(ValueError, match="verb must be"):
        demo.demo({"verb": "dance", "workspace_id": workspace_id})


def test_the_world_without_a_scope_carries_the_collectors_scopes():
    """The benchmark's world looks like a real scan's rows: the mapper's sources and scopes."""
    world = synthetic.generate(synthetic.WorldSpec(resources=200, regions=2, org_id=ORG))
    sources = {(n.source, n.scope.split(":", 1)[0]) for n in world.nodes}
    assert sources == {("aws_scan", "aws"), ("git_scan", "repo"), ("git_scan", "tf_root_module"), ("tf_state", "state_file")}
    assert all("synthetic" not in n.attributes for n in world.nodes)
    assert synthetic.world_scope(7) == "demo:seed:7" and synthetic.is_synthetic_scope("demo:seed:7") and not synthetic.is_synthetic_scope("aws:full")


def test_the_world_carries_software_and_exposure_the_way_the_collectors_would():
    from infra_map.synthetic import WorldSpec, generate

    world = generate(WorldSpec(resources=1000, seed=7))
    by_type = {}
    for node in world.nodes:
        by_type.setdefault(node.type, []).append(node)
    task_defs = by_type["ecs_task_definition"]
    assert all(n.attributes["software"][0]["purl"].startswith("pkg:docker/") and n.attributes["images"][0]["digest"].startswith("sha256:") for n in task_defs)
    assert any(s["purl"] == "pkg:docker/php@8.3.30-fpm?final=true&stage=0" for n in task_defs for s in n.attributes["software"]), "php2shell's worked example is in the world"
    assert all("exposed" in n.attributes for n in task_defs + by_type["ecs_service"] + by_type["lambda_function"] + by_type["ec2_instance"])
    assert any(n.attributes["exposed"] for n in task_defs) and not all(n.attributes["exposed"] for n in task_defs)
    lambdas = by_type["lambda_function"]
    assert any("bref-layer" in s["purl"] for n in lambdas for s in n.attributes["software"]) and any(n.attributes["exposed"] for n in lambdas)
    assert all(n.attributes["software"][0]["purl"].startswith("pkg:generic/ami@ami-") for n in by_type["ec2_instance"])
    assert any(n.attributes["exposed"] for n in by_type["ec2_instance"])
    dockerfiles = by_type["github_dockerfile"]
    assert dockerfiles and all(n.attributes["software"][0]["relation"] == "base" for n in dockerfiles)
    again = generate(WorldSpec(resources=1000, seed=7))
    assert [n.attributes.get("software") for n in again.nodes] == [n.attributes.get("software") for n in world.nodes], "deterministic from the seed"
