"""An existing map moves from DynamoDB into the map store once: copied on the first run,
skipped after, skipped when the daemon scanned first, the table left as it was, the
config naming the store it went into (the SQLite file by default, Postgres when the
workspace says so), and the rollback switch reads the table again."""

from __future__ import annotations

import os
import uuid

import boto3
import pytest
from moto import mock_aws

from compass_core import migrations
from infra_map.graph_store import open_store
from infra_map.migrate import dynamodb_to_store
from infra_map.synthetic import WorldSpec, generate

ORG = "acme"


def _config(tmp_path):
    return {"org_id": ORG, "dynamodb_config": {"workspace_id": "ws", "map": "compass_map_ws", "items": "compass_items_ws"},
            "map": {"path": str(tmp_path / "map.sqlite")}}


def _old_map(config, session, resources=40):
    """A world written to the DynamoDB map, as an install from before 0.6 left it."""
    from compass_core.commands import create_tables
    from infra_map.dynamo_store import DynamoGraphStore

    create_tables(config, client=session.client("dynamodb"))
    world = generate(WorldSpec(resources=resources, regions=1, org_id=ORG))
    old = DynamoGraphStore(table_name=config["dynamodb_config"]["map"], workspace_id=config["dynamodb_config"]["workspace_id"], session=session)
    with old.begin_scan(ORG) as scan:
        scan.put_nodes(world.nodes)
        scan.put_edges(world.edges)
    return world, old


def test_the_map_is_copied_once_and_the_table_stays(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        config = _config(tmp_path)
        world, old = _old_map(config, session)
        saved = {}
        result = dynamodb_to_store(config, workspace_id="ws", session=session, save=lambda ws, c: saved.update({ws: c}))
        assert result["status"] == "copied" and result["copied_nodes"] == world.node_count and result["copied_edges"] == world.edge_count
        assert result["store"] == "sqlite" and result["path"] == str(tmp_path / "map.sqlite")
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

        assert dynamodb_to_store(config, workspace_id="ws", session=session)["status"] == "sqlite already has a scan"
        assert old.summary(ORG).nodes == world.node_count  # the table is untouched

        # the rollback switch: the same config reads the table again
        monkeypatch.setenv("COMPASS_MAP_STORE", "dynamodb")
        assert type(open_store(config, workspace_id="ws", session=session)).__name__ == "DynamoGraphStore"


@pytest.fixture
def postgres_dsn():
    # never the network in CI: only with a database someone pointed us at
    dsn = os.getenv("COMPASS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("COMPASS_TEST_POSTGRES_DSN is not set")
    return dsn


@pytest.mark.parametrize("how", ["map.store", "COMPASS_MAP_STORE"])
def test_a_workspace_on_postgres_gets_its_map_copied_there_and_keeps_the_setting(tmp_path, monkeypatch, postgres_dsn, how):
    """The product's deployment: the services name Postgres (in the config, or by the env
    every task sets) and must not be moved to a SQLite file nobody shares."""
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    workspace_id = f"ws-{uuid.uuid4().hex[:8]}"
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        config = {"org_id": ORG, "dynamodb_config": {"workspace_id": workspace_id, "map": "compass_map_pg", "items": "compass_items_pg"},
                  "map": {"store": "postgres", "dsn": postgres_dsn}}
        if how == "COMPASS_MAP_STORE":
            config["map"] = {}
            monkeypatch.setenv("COMPASS_MAP_STORE", "postgres")
            monkeypatch.setenv("COMPASS_MAP_DSN", postgres_dsn)
        world, old = _old_map(config, session, resources=30)
        saved = {}
        try:
            result = dynamodb_to_store(config, workspace_id=workspace_id, session=session, save=lambda ws, c: saved.update({ws: c}))
            assert result["status"] == "copied" and result["store"] == "postgres" and "path" not in result
            assert result["copied_nodes"] == world.node_count and result["copied_edges"] == world.edge_count
            assert saved[workspace_id]["map"]["store"] == "postgres"
            if how == "map.store":
                assert saved[workspace_id]["map"]["dsn"] == postgres_dsn
            assert not (tmp_path / "map.sqlite").exists() and not os.path.exists(os.path.join("data", "map", f"{workspace_id}.sqlite"))

            new = open_store(config, workspace_id=workspace_id)
            assert type(new).__name__ == "PostgresGraphStore"
            assert new.summary(ORG).nodes == world.node_count and new.summary(ORG).edges == world.edge_count
            assert new.list_scans(ORG)[0].job_id == "migration:dynamodb"
            assert old.summary(ORG).nodes == world.node_count  # the table is untouched

            # a second run finds the scan and copies nothing
            second = dynamodb_to_store(config, workspace_id=workspace_id, session=session)
            assert second["status"] == "postgres already has a scan" and second["copied_nodes"] == 0
            assert config["map"]["store"] == "postgres"
        finally:
            store = open_store(config, workspace_id=workspace_id)
            with store.conn().raw.cursor() as cur:
                for table in ("nodes", "edges", "scans"):
                    cur.execute(f"DELETE FROM {table} WHERE workspace_id = %s", (workspace_id,))
            store.close()


def test_a_workspace_the_daemon_already_scanned_is_only_marked(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPASS_MAP_STORE", raising=False)
    config = _config(tmp_path)
    monkeypatch.setenv("COMPASS_MAP_STORE", "sqlite")
    store = open_store(config, workspace_id="ws")
    with store.begin_scan(ORG) as scan:
        scan.put_nodes(generate(WorldSpec(resources=20, regions=1, org_id=ORG)).nodes)
    store.close()
    monkeypatch.delenv("COMPASS_MAP_STORE")
    result = dynamodb_to_store(config, workspace_id="ws")
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
        assert first["migrations"] == ["0.6.0-map-sqlite", "0.7.0-access-model", "0.7.0-grants"]
        assert any(c.get("map", {}).get("store") == "sqlite" for _, c in saved), "the 0.6.0 step switched the map to the file"
        second = migrations.run(config, workspace_id="ws", client=client, table=table)
        assert second["migrations"] == []

        # a step sees the workspace id even when the config came from a loader that does not set it
        seen = []
        config.pop("workspace_id", None)
        migrations.run(config, workspace_id="ws", client=client, table=table, steps=[("9.9.9-test", lambda c: seen.append(c.get("workspace_id")))])
        assert seen == ["ws"]
        # an operator runs a recorded step again
        assert migrations.run(config, workspace_id="ws", client=client, table=table, steps=[("9.9.9-test", lambda c: seen.append("again"))])["migrations"] == []
        again = migrations.run(config, workspace_id="ws", client=client, table=table, steps=[("9.9.9-test", lambda c: seen.append("again"))], rerun=["9.9.9-test"])
        assert again["migrations"] == ["9.9.9-test"] and seen == ["ws", "again"]
