"""What the SQLite store promises beyond the contract: WAL so a reader in another
connection sees the previous graph until the scan commits, a second scan is refused
while one runs, a backup is a consistent copy, history answers as-of and diff, and
prune keeps what retention says."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from infra_map.graph_store import NodeQuery, ScanInProgress
from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.sql_store import SqliteGraphStore

ORG = "acme"


def _node(rid: str, ntype: str = "lambda_function", **attrs) -> ResourceNode:
    return ResourceNode(org_id=ORG, resource_id=rid, type=ntype, account_id="123456789012", region="us-east-1", name=rid, attributes=attrs,
                        source="aws_scan", scope="123", last_seen=datetime(2026, 1, 1, tzinfo=timezone.utc))


def _edge(src: str, dst: str, rel: str = "depends_on") -> RelationshipEdge:
    return RelationshipEdge(org_id=ORG, from_resource_id=src, to_resource_id=dst, relationship_type=rel, source="aws_scan", scope="123")


def _write(store, nodes, edges=()):
    with store.begin_scan(ORG) as scan:
        scan.put_nodes(nodes)
        scan.put_edges(edges)
    return scan.scan


def test_the_file_is_in_wal_mode_and_a_reader_sees_the_old_graph_until_commit(tmp_path):
    path = tmp_path / "map.sqlite"
    writer = SqliteGraphStore(path, "ws")
    reader = SqliteGraphStore(path, "ws")
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    _write(writer, [_node("a")])
    seen_during = {}

    def read():
        seen_during["node"] = reader.get_node(ORG, "b")
        seen_during["count"] = reader.summary(ORG).nodes

    scan = writer.begin_scan(ORG)
    scan.put_nodes([_node("a"), _node("b")])
    t = threading.Thread(target=read)
    t.start()
    t.join(timeout=5)
    assert seen_during == {"node": None, "count": 1}  # the scan is open; the reader did not block and saw the previous graph
    scan.commit()
    assert reader.get_node(ORG, "b") is not None and reader.summary(ORG).nodes == 2
    writer.close()
    reader.close()


def test_a_second_scan_is_refused_while_one_is_open_and_an_abandoned_one_is_taken_over(tmp_path):
    store = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
    other = SqliteGraphStore(tmp_path / "map.sqlite", "ws", busy_timeout_ms=200)
    first = store.begin_scan(ORG)
    with pytest.raises((ScanInProgress, sqlite3.OperationalError)):
        other.begin_scan(ORG)
    first.abort("test")
    assert store.list_scans(ORG) == []  # an aborted scan is not a completed one
    # a running row older than the stale limit is taken over
    store.conn().execute("INSERT INTO scans (workspace_id, org_id, started_at, status) VALUES ('ws', ?, ?, 'running')", (ORG, int(time.time() * 1000) - 3 * 3600 * 1000))
    _write(store, [_node("a")])
    assert [s.status for s in store.list_scans(ORG)] == ["completed"]
    store.close()
    other.close()


def test_history_answers_as_of_and_diff_and_prune_keeps_what_retention_says(tmp_path):
    store = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
    s1 = _write(store, [_node("a", size=1), _node("b"), _node("c")], [_edge("a", "b"), _edge("a", "c")])
    time.sleep(0.002)
    s2 = _write(store, [_node("a", size=2), _node("b"), _node("d")], [_edge("a", "b"), _edge("a", "d")])
    assert s2.changed == 1 and s2.added == 2 and s2.removed == 2
    assert store.get_node(ORG, "a").attributes == {"size": 2}
    assert store.get_node(ORG, "a", as_of=s1.started_at).attributes == {"size": 1}
    assert store.get_node(ORG, "c", as_of=s1.started_at) is not None and store.get_node(ORG, "c") is None
    assert store.summary(ORG, as_of=s1.started_at).nodes == 3 and store.summary(ORG).nodes == 3
    assert store.query_nodes(ORG, NodeQuery(changed_since=s1.started_at)).total == 2  # a's new version and d

    diff = store.diff(ORG, s1.scan_id, s2.scan_id)
    by = {(i["kind"], i["id"]): i for i in diff.items}
    assert by[("node", "a")]["change"] == "changed" and by[("node", "a")]["changed_keys"] == ["attributes.size"]
    assert by[("node", "c")]["change"] == "removed" and by[("node", "d")]["change"] == "added"
    assert by[("edge", _edge("a", "c").canonical_edge_id())]["change"] == "removed"
    assert len(diff.items) == 5 and diff.next_cursor is None
    page = store.diff(ORG, s1.scan_id, s2.scan_id, limit=2)
    assert len(page.items) == 2 and page.next_cursor
    assert [s.scan_id for s in store.list_scans(ORG)] == [s2.scan_id, s1.scan_id]

    assert store.prune(ORG, keep_days=365, keep_scans=1) == 0   # a day old at most: nothing to drop
    assert store.prune(ORG, keep_days=0, keep_scans=5) == 0     # fewer than five scans: everything stays
    assert store.prune(ORG, keep_days=0, keep_scans=1) == 0     # the last scan's diff still needs what it closed
    time.sleep(0.002)
    assert store.prune(ORG, keep_days=0, keep_scans=0) == 3     # a's old version, c, a->c
    assert store.get_node(ORG, "a", as_of=s1.started_at) is None and store.summary(ORG).nodes == 3
    store.close()


def test_a_backup_is_a_consistent_copy_that_opens_as_a_store(tmp_path):
    store = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
    _write(store, [_node("a"), _node("b")], [_edge("a", "b")])
    out = store.backup(tmp_path / "backups" / "map.sqlite")
    copy = SqliteGraphStore(out, "ws")
    assert copy.summary(ORG).nodes == 2 and copy.summary(ORG).edges == 1
    copy.close()
    store.close()


def test_first_seen_is_when_the_collector_saw_it_and_survives_a_change(tmp_path):
    store = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
    _write(store, [_node("a", v=1)])
    first = store.conn().execute("SELECT first_seen FROM nodes WHERE node_id = 'a'").fetchone()[0]
    assert first == int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    _write(store, [_node("a", v=2)])
    rows = store.conn().execute("SELECT first_seen, valid_to FROM nodes WHERE node_id = 'a' ORDER BY row_id").fetchall()
    assert [r[0] for r in rows] == [first, first] and rows[0][1] is not None and rows[1][1] is None
    store.close()


def test_last_seen_is_when_the_collector_saw_it_not_when_the_scan_wrote(tmp_path):
    store = SqliteGraphStore(tmp_path / "map.sqlite", "ws")
    seen_monday = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    seen_tuesday = datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc)
    a = _node("a")
    a.last_seen = seen_monday
    _write(store, [a, _node("b")])
    assert store.get_node(ORG, "a").last_seen == seen_monday
    a.last_seen = seen_tuesday  # unchanged content, seen again a day later
    second = _write(store, [a])
    assert store.get_node(ORG, "a").last_seen == seen_tuesday and second.changed == 0
    assert store.get_node(ORG, "b") is None and second.removed == 1  # what decides closing is the scan, not the clock
    store.close()
