"""An existing map moves from DynamoDB to SQLite once: copied on the first run, skipped
after, skipped when the daemon scanned first, the table left as it was, the config
flipped, and the rollback switch reads the table again."""

from __future__ import annotations

import boto3
from moto import mock_aws

from compass_core import migrations
from infra_map.graph_store import open_store
from infra_map.migrate import dynamodb_to_sqlite
from tests.benchmark.world import WorldSpec, generate

ORG = "acme"


def _config(tmp_path):
    return {"org_id": ORG, "dynamodb_config": {"workspace_id": "ws", "map": "compass_map_ws", "items": "compass_items_ws"},
            "map": {"path": str(tmp_path / "map.sqlite")}}


def test_the_map_is_copied_once_and_the_table_stays(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    with mock_aws():
        from compass_core.commands import create_tables

        session = boto3.Session(region_name="us-east-1")
        config = _config(tmp_path)
        create_tables(config, client=session.client("dynamodb"))
        world = generate(WorldSpec(resources=40, regions=1, org_id=ORG))
        old = open_store(config, workspace_id="ws", session=session)
        with old.begin_scan(ORG) as scan:
            scan.put_nodes(world.nodes)
            scan.put_edges(world.edges)
        saved = {}
        result = dynamodb_to_sqlite(config, workspace_id="ws", session=session, save=lambda ws, c: saved.update({ws: c}))
        assert result["status"] == "copied" and result["copied_nodes"] == world.node_count and result["copied_edges"] == world.edge_count
        assert saved["ws"]["map"]["store"] == "sqlite" and config["map"]["store"] == "sqlite"

        new = open_store(config, workspace_id="ws")
        assert new.summary(ORG).nodes == world.node_count
        # DynamoDB hands numbers back as Decimal: they arrive as numbers, not strings
        managed = new.get_node(ORG, world.managed[0])
        assert managed.attributes["coverage_score"] == 1 and isinstance(managed.attributes["coverage_score"], int)
        function = next(n for n in world.nodes if n.type == "lambda_function")
        assert isinstance(new.get_node(ORG, function.resource_id).attributes["memorySize"], int)
        from compass_api.routes.maps import _iac_coverage_summary

        assert _iac_coverage_summary(ORG, new)["covered"] == len(world.managed)
        # the old map's freshness survives: last_seen is when the old scan saw it, not when it was copied
        assert new.get_node(ORG, function.resource_id).last_seen == function.last_seen.replace(microsecond=function.last_seen.microsecond // 1000 * 1000)
        scan = new.list_scans(ORG)[0]
        assert scan.job_id == "migration:dynamodb" and scan.node_count == world.node_count
        first_seen = new.conn().execute("SELECT MIN(first_seen), MAX(first_seen) FROM nodes").fetchone()
        assert first_seen[0] == first_seen[1] == int(world.nodes[0].last_seen.timestamp() * 1000)
        new.close()

        assert dynamodb_to_sqlite(config, workspace_id="ws", session=session)["status"] == "already sqlite"
        assert old.summary(ORG).nodes == world.node_count  # the table is untouched

        # the rollback switch: the same config reads the table again
        monkeypatch.setenv("COMPASS_MAP_STORE", "dynamodb")
        assert type(open_store(config, workspace_id="ws", session=session)).__name__ == "DynamoGraphStore"


def test_a_workspace_the_daemon_already_scanned_is_only_marked(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    config = _config(tmp_path)
    monkeypatch.setenv("COMPASS_MAP_STORE", "sqlite")
    store = open_store(config, workspace_id="ws")
    with store.begin_scan(ORG) as scan:
        scan.put_nodes(generate(WorldSpec(resources=20, regions=1, org_id=ORG)).nodes)
    store.close()
    monkeypatch.delenv("COMPASS_MAP_STORE")
    result = dynamodb_to_sqlite(config, workspace_id="ws")
    assert result["status"] == "sqlite already has a scan" and config["map"]["store"] == "sqlite"


def test_compass_migrate_runs_the_step_once_per_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    with mock_aws():
        from compass_core import config as config_module
        from compass_core.commands import create_tables

        session = boto3.Session(region_name="us-east-1")
        config = _config(tmp_path)
        client = session.client("dynamodb")
        create_tables(config, client=client)
        saved = []
        monkeypatch.setattr(config_module, "save_workspace_config", lambda ws, c: saved.append((ws, dict(c))))
        table = session.resource("dynamodb").Table("compass_items_ws")
        first = migrations.run(config, workspace_id="ws", client=client, table=table)
        assert first["migrations"] == ["0.6.0-map-sqlite"] and saved[-1][1]["map"]["store"] == "sqlite"
        second = migrations.run(config, workspace_id="ws", client=client, table=table)
        assert second["migrations"] == []
