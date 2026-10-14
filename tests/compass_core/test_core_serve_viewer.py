"""`compass serve map_port=…` serves the map viewer from the daemon itself."""

import threading
import urllib.request

from compass_core import config as core_config
from compass_core import queue, serve


def test_serve_writes_and_serves_the_map_page(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "_loader", lambda ws: {"workspace_id": ws, "org_id": ws})
    monkeypatch.setattr("captains.prs.scheduler.tick", lambda workspaces, client=None: 0)
    monkeypatch.setattr(
        "captains.mapper.export.write_map",
        lambda config, *, org_id, workspace_id, fmt, out: (open(out, "w").write("<html>the map</html>") and None) or {"out": out, "nodes": 3, "edges": 2},
    )
    previous = queue._factory
    server = serve.Server("ws-1", {"workspace_id": "ws-1", "org_id": "ws-1"}, prs_tick=0, once=False, map_port=0, map_dir=str(tmp_path / "map"))
    # Pick a free port by binding 0, then read it back.
    server.map_port = 0
    try:
        server._start_map_viewer()
        port = server._httpd.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        assert body == "<html>the map</html>"
    finally:
        server.stop()
        queue.set_job_queue(previous)


def test_viewer_has_a_placeholder_until_a_map_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("captains.mapper.export.write_map", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no map")))
    server = serve.Server("ws-1", {"workspace_id": "ws-1"}, map_port=0, map_dir=str(tmp_path / "map"))
    try:
        server._start_map_viewer()
        port = server._httpd.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        assert "No map yet" in body
    finally:
        server.stop()
    assert isinstance(threading.current_thread(), threading.Thread)
