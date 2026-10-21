"""Which store a workspace gets, and that the SQLite one is shared: a new install is on
SQLite, an old config without `map` stays on DynamoDB until `compass migrate` moves it,
COMPASS_MAP_STORE overrides both, the API's request threads share one store per file,
and `mapper backup` copies the file while it is open."""

from __future__ import annotations

import threading

import pytest

from infra_map import graph_store
from infra_map.graph_store import backend_name, open_store


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    yield
    graph_store.close_shared_stores()


def test_the_config_names_the_store_and_the_environment_overrides_it(monkeypatch):
    assert backend_name({"map": {"store": "sqlite"}}) == "sqlite"
    assert backend_name({"dynamodb_config": {"map": "t"}}) == "dynamodb"  # an install from before 0.6, until compass migrate
    monkeypatch.setenv("COMPASS_MAP_STORE", "dynamodb")
    assert backend_name({"map": {"store": "sqlite"}}) == "dynamodb"     # the rollback switch
    monkeypatch.setenv("COMPASS_MAP_STORE", "nope")
    with pytest.raises(LookupError, match="sqlite, dynamodb, postgres"):
        open_store({}, workspace_id="ws")


def test_threads_share_one_store_per_file_and_closing_it_closes_nothing(tmp_path):
    config = {"map": {"store": "sqlite"}}
    stores = []

    def open_one():
        stores.append(open_store(config, workspace_id="ws"))

    threads = [threading.Thread(target=open_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in stores}) == 1
    first = stores[0]
    with first.begin_scan("acme") as scan:
        scan.put_nodes([])
    first.close()  # a caller closing what it was given must not close the others' connections
    assert first.list_scans("acme")[0].status == "completed"
    assert (tmp_path / "map" / "ws.sqlite").exists()


def test_mapper_backup_copies_the_file_while_it_is_open(tmp_path, monkeypatch):
    from captains.mapper import commands
    from compass_core.workspace import Workspace
    from infra_map.synthetic import WorldSpec, generate

    config = {"map": {"store": "sqlite"}, "org_id": "acme", "workspace_id": "ws"}
    world = generate(WorldSpec(resources=30, regions=1, org_id="acme"))
    store = open_store(config, workspace_id="ws")
    with store.begin_scan("acme") as scan:
        scan.put_nodes(world.nodes)
        scan.put_edges(world.edges)
    monkeypatch.setattr(commands, "resolve", lambda _p: Workspace(workspace_id="ws", config=config))
    out = commands.backup({"out": str(tmp_path / "copy.sqlite")})
    assert out["bytes"] > 0 and out["out"].endswith("copy.sqlite")
    from infra_map.sql_store import SqliteGraphStore

    copy = SqliteGraphStore(out["out"], "ws")
    assert copy.summary("acme").nodes == world.node_count
    copy.close()

    monkeypatch.setattr(commands, "resolve", lambda _p: Workspace(workspace_id="ws", config={"dynamodb_config": {"map": "t"}}))
    with pytest.raises(ValueError, match="not sqlite"):
        commands.backup({})
