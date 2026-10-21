"""The map routes as they answer today, against a real store on moto: these pin the
responses so the store behind them can change without the API changing.

/api/maps whole and focused, /api/iac/coverage, /api/iac/suggestions,
/api/honeypots/suggestions, /api/honeypots/repos, /api/maps/ownership and
/api/librarian/terraform-location, over a small synthetic world
(tests/benchmark/world.py), the same one the benchmark scales up."""

from __future__ import annotations

import importlib
from typing import Any, Dict

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from infra_map.synthetic import WorldSpec, generate

ORG = "acme"
WORKSPACE = "ws-1"
TABLE = "compass_map_ws-1"


def _write_world(store, world) -> None:
    with store.begin_scan(ORG) as scan:
        scan.put_nodes(world.nodes)
        scan.put_edges(world.edges)


@pytest.fixture
def world():
    return generate(WorldSpec(resources=60, regions=2, org_id=ORG, seed=3))


@pytest.fixture(params=["dynamodb", "sqlite"])
def client(request, monkeypatch, world, tmp_path):
    """The three routers that read the map, over ``world`` in each store: the same answers
    from both is what lets the default change without the API changing."""
    from infra_map import graph_store

    monkeypatch.setenv("COMPASS_MAP_STORE", request.param)
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    with mock_aws():
        from compass_core.commands import create_tables
        from infra_map.graph_store import open_store

        session = boto3.Session(region_name="us-east-1")
        create_tables({"dynamodb_config": {"map": TABLE}}, client=session.client("dynamodb"))
        config = {"dynamodb_config": {"map": TABLE}}
        _write_world(open_store(config, workspace_id=WORKSPACE, session=session), world)

        def ctx(_request) -> Dict[str, Any]:
            return {"store": open_store(config, workspace_id=WORKSPACE, session=session), "default_org": ORG, "workspace_id": WORKSPACE}

        app = FastAPI()
        for name in ("maps", "honeypots", "librarian"):
            module = importlib.import_module(f"compass_api.routes.{name}")
            monkeypatch.setattr(module, "resolve_infra_map_context", ctx)
            app.include_router(module.router)
        monkeypatch.setattr(importlib.import_module("compass_api.routes.honeypots"), "get_workspace_config", lambda _r: {"github": {}})
        yield TestClient(app)
    graph_store.close_shared_stores()


def test_the_whole_map_lists_every_node_and_edge_with_their_shape(client, world):
    body = client.get("/api/maps").json()
    assert body["org_id"] == ORG and body["workspace_id"] == WORKSPACE and body["focus"] is None
    assert len(body["nodes"]) == world.node_count
    assert len(body["edges"]) == world.edge_count
    node = next(n for n in body["nodes"] if n["resource_type"] == "lambda_function")
    assert {"org_id", "resource_id", "resource_type", "account_id", "region", "name", "tags", "attributes", "confidence", "last_seen", "source", "scope"} <= set(node)
    assert "record_type" not in node
    edge = body["edges"][0]
    assert {"from_resource_id", "to_resource_id", "relationship_type", "edge_id", "resource_id", "evidence", "source", "scope"} <= set(edge)
    assert edge["resource_id"] == edge["edge_id"]


def test_a_limit_cuts_the_whole_map_at_that_many_items_without_saying_so(client, world):
    body = client.get("/api/maps", params={"limit": 100}).json()
    assert len(body["nodes"]) + len(body["edges"]) == 100
    assert "truncated" not in body


def test_a_focus_returns_the_neighbourhood_at_the_asked_depth(client, world):
    hub = world.hubs[0]
    body = client.get("/api/maps", params={"focus": hub, "depth": 1}).json()
    assert body["focus"] == hub
    ids = {n["resource_id"] for n in body["nodes"]}
    expected = {hub} | {e.from_resource_id for e in world.edges if e.to_resource_id == hub} | {e.to_resource_id for e in world.edges if e.from_resource_id == hub}
    assert ids == expected
    assert all(e["from_resource_id"] == hub or e["to_resource_id"] == hub for e in body["edges"])
    assert len(body["edges"]) == len([e for e in world.edges if hub in (e.from_resource_id, e.to_resource_id)])


def test_iac_coverage_counts_managed_unmanaged_and_drifted_aws_resources(client, world):
    body = client.get("/api/iac/coverage").json()
    aws = [n for n in world.nodes if n.source == "aws_scan"]
    managed = [n for n in aws if n.attributes.get("coverage_score") == 1]
    drifted = [n for n in managed if n.attributes.get("drift_status") == "drifted"]
    assert body["total"] == len(aws) and body["covered"] == len(managed)
    assert body["unmanaged"] == len(aws) - len(managed) and body["drifted"] == len(drifted)
    assert body["coverage_pct"] == round(len(managed) / len(aws) * 100, 1)
    assert len(body["unmanaged_resources"]) == body["unmanaged"]
    assert {"resource_id", "resource_type", "name", "region", "account_id"} == set(body["unmanaged_resources"][0])
    totals = [row["total"] for row in body["by_type"]]
    assert totals == sorted(totals, reverse=True)
    assert {row["type"]: row["total"] for row in body["by_type"]} == {t: c for t, c in world.by_type().items() if t in {n.type for n in aws}}
    if drifted:
        assert body["drifted_resources"][0]["drift_fields"][0]["field"] == "tags.Environment"


def test_iac_suggestions_list_the_repos_and_root_modules_in_the_map(client, world):
    body = client.get("/api/iac/suggestions").json()
    repos = sorted({n.name for n in world.nodes if n.type == "github_repo"})
    modules = sorted(({"repo": n.attributes["repo"], "path": n.attributes["path"]} for n in world.nodes if n.type == "tf_root_module"), key=lambda m: (m["repo"], m["path"]))
    assert body == {"repos": repos, "modules": modules}


def test_honeypot_suggestions_come_from_the_map_sorted_by_priority(client, world):
    body = client.get("/api/honeypots/suggestions").json()
    assert body and len(body) <= 50
    priorities = [{"critical": 0, "high": 1, "medium": 2, "low": 3}[s["priority"]] for s in body]
    assert priorities == sorted(priorities)
    secret = next(s for s in body if s["resource_type"] == "secretsmanager_secret")
    assert secret["recommended_class"] and secret["placement"] is None and secret["default_config"]["severity"]
    assert {s["resource_id"] for s in body} <= {n.resource_id for n in world.nodes}


def test_honeypot_repos_count_the_root_modules_per_repo(client, world):
    body = client.get("/api/honeypots/repos").json()
    counts: Dict[str, int] = {}
    for n in world.nodes:
        if n.type == "tf_root_module":
            counts[n.attributes["repo"]] = counts.get(n.attributes["repo"], 0) + 1
    assert body["repos"][0] == {"repo": max(counts, key=lambda r: (counts[r], -ord(r[-1]))), "module_count": max(counts.values())} or body["repos"][0]["module_count"] == max(counts.values())
    assert {r["repo"] for r in body["repos"]} == set(counts) | {n.name for n in world.nodes if n.type == "github_repo"}
    assert body["default_repo"] == ""


def test_ownership_walks_observes_contains_writes_to_the_root_module(client, world):
    managed = world.managed[0]
    instance = next(e.from_resource_id for e in world.edges if e.relationship_type == "observes" and e.to_resource_id == managed)
    state_file = next(e.from_resource_id for e in world.edges if e.relationship_type == "contains" and e.to_resource_id == instance)
    root = next(e.from_resource_id for e in world.edges if e.relationship_type == "writes" and e.to_resource_id == state_file)
    body = client.get("/api/maps/ownership", params={"resource_id": managed}).json()
    assert body["status"] == "partial"  # the world has no tf_address nodes, so the file is not found
    assert body["answer"]["tf_root_module_id"] == root and body["answer"]["tf_state_file_id"] == state_file
    assert body["answer"]["repo"] is None  # today the repo comes only through a declaring github_file
    assert body["candidates"] and body["path"] and body["freshness"]["freshness_class"]

    unmanaged = next(n.resource_id for n in world.nodes if n.source == "aws_scan" and n.attributes.get("coverage_score") == 0)
    assert client.get("/api/maps/ownership", params={"resource_id": unmanaged}).json()["status"] == "unknown"


def test_the_librarian_finds_the_terraform_location_the_same_way(client, world):
    managed = world.managed[1]
    body = client.get("/api/librarian/terraform-location", params={"resource_id": managed}).json()
    assert body["workspace_id"] == WORKSPACE and body["org_id"] == ORG
    assert body["status"] in ("partial", "verified") and body["answer"]["tf_root_module_id"].startswith("tfroot://")
