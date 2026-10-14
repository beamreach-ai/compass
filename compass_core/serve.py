"""`compass serve`: the open version running unattended.

One process, three loops, no broker:

- alert consumers: one thread per decoy alert queue (``honeypot.alert_queue_urls``
  or ``alert_queue_url`` in the workspace config, else HONEYPOT_ALERT_QUEUE_URLS);
  each alert is recorded, then investigate and respond run in-process through
  the open dispatcher;
- the PR watcher tick every ``prs_tick`` seconds (default 300; 0 = off);
- a map re-scan every ``map_interval`` seconds (default 0 = off).

Stops on SIGINT/SIGTERM. The product runs the same loops as Celery workers and
an API-side ticker.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from compass_core import dispatch
from compass_core.cli import COMMAND_MODULES, load_commands
from compass_core.workspace import captain_config, resolve

logger = logging.getLogger("compass.serve")


def queue_urls(config: Dict[str, Any]) -> List[str]:
    hp = config.get("honeypot") if isinstance(config.get("honeypot"), dict) else {}
    urls = hp.get("alert_queue_urls") or ([hp["alert_queue_url"]] if hp.get("alert_queue_url") else [])
    if not urls:
        raw = os.getenv("HONEYPOT_ALERT_QUEUE_URLS") or os.getenv("HONEYPOT_ALERT_QUEUE_URL") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
    return [str(u) for u in urls]


class Server:
    def __init__(
        self,
        workspace_id: str,
        config: Dict[str, Any],
        *,
        prs_tick: int = 300,
        map_interval: int = 0,
        once: bool = False,
        map_port: int = 0,
        map_dir: str = "map",
        job_poll: float = 2.0,
    ) -> None:
        self.workspace_id = workspace_id
        self.config = config
        self.prs_tick = max(0, int(prs_tick))
        self.map_interval = max(0, int(map_interval))
        self.once = once
        self.map_port = max(0, int(map_port))
        self.map_dir = Path(map_dir)
        self.job_poll = max(0.0, float(job_poll))
        self._stop = threading.Event()
        self._consumers: List[Any] = []
        self._threads: List[threading.Thread] = []
        self._httpd: Any = None
        self.ticks = 0

    def stop(self, *_args: Any) -> None:
        self._stop.set()
        for consumer in self._consumers:
            consumer.stop()
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    # ── the map viewer ──────────────────────────────────────────────────────

    def _write_map_page(self) -> None:
        """index.html from the current map; skipped (with a note) until a map exists."""
        from captains.mapper.export import write_map

        try:
            self.map_dir.mkdir(parents=True, exist_ok=True)
            out = write_map(self.config, org_id=str(self.config.get("org_id") or self.workspace_id), workspace_id=self.workspace_id, fmt="html", out=str(self.map_dir / "index.html"))
            logger.info("map viewer page written: %s nodes, %s edges", out["nodes"], out["edges"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("map viewer page not written (%s); run `compass mapper run` first", exc)

    def _start_map_viewer(self) -> None:
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        self._write_map_page()
        self.map_dir.mkdir(parents=True, exist_ok=True)
        if not (self.map_dir / "index.html").exists():
            (self.map_dir / "index.html").write_text(
                "<!doctype html><title>Compass</title><p>No map yet. Run <code>compass mapper run</code>, then reload.</p>",
                encoding="utf-8",
            )
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.map_dir))
        handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.map_port), handler)
        thread = threading.Thread(target=self._httpd.serve_forever, name="map-viewer", daemon=True)
        thread.start()
        self._threads.append(thread)
        logger.info("map viewer on http://localhost:%d/", self.map_port)

    def _start_consumers(self) -> None:
        from captains.honeypot.alert_consumer import AlertConsumer

        urls = queue_urls(self.config)
        if not urls:
            logger.warning("no decoy alert queue configured (honeypot.alert_queue_url); alerts will not be consumed")
        for url in urls:
            consumer = AlertConsumer(queue_url=url)
            thread = threading.Thread(target=consumer.run, kwargs={"install_signals": False}, name=f"alerts:{url.rsplit('/', 1)[-1]}", daemon=True)
            thread.start()
            self._consumers.append(consumer)
            self._threads.append(thread)
            logger.info("consuming decoy alerts from %s", url)

    def _tick_prs(self) -> None:
        from captains.prs.scheduler import tick

        try:
            enqueued = tick([self.workspace_id])
            logger.info("prs tick: %d sync(s) ran", enqueued)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prs tick failed: %s", exc)

    # ── jobs the API queued ─────────────────────────────────────────────────

    def _run_queued_jobs(self) -> int:
        """Claim and run the jobs the API wrote as ``queued`` (oldest first). Returns how many ran."""
        from compass_core.events import job_event_context
        from compass_core.jobs import heartbeat, store_for

        store = store_for(self.config)
        if store is None:
            return 0
        try:
            queued = [j for j in store.list_jobs(limit=50, workspace_id=self.workspace_id) if str(j.get("status")) == "queued"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot list queued jobs: %s", exc)
            return 0
        ran = 0
        for job in sorted(queued, key=lambda j: j.get("created_at", 0)):
            if self._stop.is_set():
                break
            job_id = str(job["job_id"])
            claimed = store.claim_running(job_id, workspace_id=self.workspace_id, worker_id="compass-serve")
            if not claimed:
                continue  # another worker took it
            payload = dict(job.get("payload") or {})
            payload.setdefault("workspace_id", self.workspace_id)
            logger.info("running queued job %s: %s.%s", job_id, job.get("captain"), job.get("command"))
            with job_event_context(job_id, self.workspace_id), heartbeat(store, job_id, workspace_id=self.workspace_id):
                try:
                    result = dispatch.dispatch(str(job.get("captain")), str(job.get("command")), payload)
                    store.mark_completed(job_id, result if isinstance(result, dict) else {"result": result}, workspace_id=self.workspace_id)
                except Exception as exc:  # noqa: BLE001 - a job failure is recorded, never fatal to serve
                    logger.warning("queued job %s failed: %s", job_id, exc)
                    store.mark_failed(job_id, str(exc), workspace_id=self.workspace_id)
            ran += 1
        return ran

    def _job_worker(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_queued_jobs()
            except Exception:  # noqa: BLE001
                logger.exception("job worker loop failed; backing off")
            self._stop.wait(self.job_poll)

    def _rescan(self) -> None:
        try:
            dispatch.dispatch("mapper", "run", {"workspace_id": self.workspace_id})
            logger.info("map re-scan completed")
            if self.map_port:
                self._write_map_page()
        except Exception as exc:  # noqa: BLE001
            logger.warning("map re-scan failed: %s", exc)

    def run(self) -> Dict[str, Any]:
        for captain in COMMAND_MODULES:
            load_commands(captain)
        dispatch.use_inline_queue()
        self._start_consumers()
        if self.map_port:
            self._start_map_viewer()
        if self.job_poll and not self.once:
            worker = threading.Thread(target=self._job_worker, name="jobs", daemon=True)
            worker.start()
            self._threads.append(worker)
            logger.info("running jobs queued by the API (every %ss)", self.job_poll)
        logger.info("compass serving workspace %s (prs tick %ss, map re-scan %ss)", self.workspace_id, self.prs_tick, self.map_interval)

        next_prs = time.monotonic() if self.prs_tick else None
        next_map = (time.monotonic() + self.map_interval) if self.map_interval else None
        while not self._stop.is_set():
            now = time.monotonic()
            if next_prs is not None and now >= next_prs:
                self._tick_prs()
                self.ticks += 1
                next_prs = now + self.prs_tick
            if next_map is not None and now >= next_map:
                self._rescan()
                next_map = now + self.map_interval
            if self.once:
                break
            self._stop.wait(1.0)
        self.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        return {"workspace_id": self.workspace_id, "ticks": self.ticks, "queues": queue_urls(self.config)}


def serve(payload: Dict[str, Any]) -> Dict[str, Any]:
    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    server = Server(
        workspace.workspace_id,
        config,
        prs_tick=int(payload.get("prs_tick", os.getenv("PRS_TICK_SECONDS", 300)) or 0),
        map_interval=int(payload.get("map_interval", os.getenv("COMPASS_MAP_INTERVAL", 0)) or 0),
        once=payload.get("once") in (True, "true", "1", 1),
        map_port=int(payload.get("map_port", os.getenv("COMPASS_MAP_PORT", 0)) or 0),
        map_dir=str(payload.get("map_dir") or os.getenv("COMPASS_MAP_DIR") or "map"),
        job_poll=float(payload.get("job_poll", os.getenv("COMPASS_JOB_POLL_SECONDS", 2.0)) or 0),
    )
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, server.stop)
        signal.signal(signal.SIGTERM, server.stop)
    return server.run()
