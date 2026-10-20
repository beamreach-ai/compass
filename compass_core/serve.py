"""`compass serve`: the open version running unattended.

One process, no broker. It runs, for every captain that is active in the
workspace, the background services its manifest declares
(compass_core.captains.ServiceSpec): the decoys' alert consumers, the PR
watcher's tick, the map re-scan when an interval is set. Beside them the core
runs the jobs the API queued (``compass_core.queue.StoreQueue``) and, with
``map_port``, a static viewer of the map.

Intervals come from the serve payload or the environment, by the setting name
the service declares (``prs_tick`` → ``PRS_TICK_SECONDS`` for compatibility,
otherwise ``COMPASS_<SETTING>``). A captain turned off while serving has its
services stopped on the next check (every ``captains_poll`` seconds).

Stops on SIGINT/SIGTERM. The product runs the same work as Celery workers and
an API-side ticker.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from compass_core import captains as registry
from compass_core import dispatch, sessions
from compass_core.captains import CaptainManifest, ServiceSpec
from compass_core.config import workspace_config
from compass_core.workspace import captain_config, resolve

logger = logging.getLogger("compass.serve")

_LEGACY_ENV = {"prs_tick": "PRS_TICK_SECONDS", "map_interval": "COMPASS_MAP_INTERVAL"}


def interval_from_env(setting: str) -> Optional[int]:
    raw = os.getenv(_LEGACY_ENV.get(setting, "")) or os.getenv(f"COMPASS_{setting.upper()}")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class _Running:
    def __init__(self, manifest: CaptainManifest, spec: ServiceSpec, stop: threading.Event, thread: threading.Thread):
        self.manifest = manifest
        self.spec = spec
        self.stop = stop
        self.thread = thread

    @property
    def key(self) -> str:
        return f"{self.manifest.name}:{self.spec.name}"


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
        intervals: Optional[Dict[str, int]] = None,
        captains_poll: float = 30.0,
    ) -> None:
        self.workspace_id = workspace_id
        self.config = config
        self.intervals: Dict[str, int] = {"prs_tick": max(0, int(prs_tick)), "map_interval": max(0, int(map_interval))}
        self.intervals.update({k: max(0, int(v)) for k, v in (intervals or {}).items()})
        self.once = once
        self.map_port = max(0, int(map_port))
        self.map_dir = Path(map_dir)
        self.job_poll = max(0.0, float(job_poll))
        self.captains_poll = max(1.0, float(captains_poll))
        self._stop = threading.Event()
        self._services: Dict[str, _Running] = {}
        self._threads: List[threading.Thread] = []
        self._httpd: Any = None
        self.ticks = 0
        self.started: List[str] = []
        self.restarting = False
        self._announced_update: Optional[str] = None
        self._started_at = time.time()

    def stop(self, *_args: Any) -> None:
        self._stop.set()
        for running in list(self._services.values()):
            running.stop.set()
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
        # the page follows the re-scan cadence, when there is one
        refresh = int(self.intervals.get("map_interval") or 0)
        if refresh > 0:
            def _refresh() -> None:
                while not self._stop.wait(refresh + 15):
                    self._write_map_page()
            t = threading.Thread(target=_refresh, name="map-viewer-refresh", daemon=True)
            t.start()
            self._threads.append(t)

    # ── the captains' services ──────────────────────────────────────────────

    def _interval_for(self, spec: ServiceSpec) -> Optional[int]:
        if spec.interval_setting and spec.interval_setting in self.intervals:
            return self.intervals[spec.interval_setting]
        if spec.interval_setting:
            from_env = interval_from_env(spec.interval_setting)
            if from_env is not None:
                return from_env
        return spec.interval

    def _wanted_services(self) -> List[tuple]:
        try:
            config = workspace_config(self.workspace_id)
        except LookupError:
            config = self.config
        wanted = []
        for name in registry.active(config):
            manifest = registry.manifest(name)
            if manifest is None:
                continue
            for spec in manifest.services:
                interval = self._interval_for(spec)
                if spec.interval_setting is not None and not interval:
                    continue  # an interval service with no interval is off
                wanted.append((manifest, spec, interval))
        return wanted

    def _current_config(self) -> Dict[str, Any]:
        """The workspace config as it is now, with the ids serve resolved at start. Settings
        written after serve started (init_alerts records the alert queue after `up -d`)
        reach a service the next time it starts."""
        try:
            fresh = dict(workspace_config(self.workspace_id))
        except LookupError:
            return self.config
        for key in ("workspace_id", "org_id", "target"):
            if key in self.config:
                fresh[key] = self.config[key]
        return fresh

    def _run_service(self, manifest: CaptainManifest, spec: ServiceSpec, interval: Optional[int], stop: threading.Event) -> None:
        try:
            start = registry.load_reference(spec.start)
        except Exception as exc:  # noqa: BLE001
            logger.warning("service %s:%s cannot start: %s", manifest.name, spec.name, exc)
            return
        try:
            start(self.workspace_id, self._current_config(), stop, interval=interval)
        except Exception as exc:  # noqa: BLE001
            logger.warning("service %s:%s failed: %s", manifest.name, spec.name, exc)

    def _reconcile_services(self) -> None:
        """Start services of captains that became active; stop those of captains turned off."""
        wanted = {f"{m.name}:{s.name}": (m, s, i) for m, s, i in self._wanted_services()}
        for key, running in list(self._services.items()):
            if key not in wanted:
                running.stop.set()
                del self._services[key]
                logger.info("stopped %s (%s is off)", key, running.manifest.label)
        for key, (manifest, spec, interval) in wanted.items():
            if key in self._services and self._services[key].thread.is_alive():
                continue
            stop = threading.Event()
            thread = threading.Thread(target=self._run_service, args=(manifest, spec, interval, stop), name=key, daemon=True)
            thread.start()
            self._services[key] = _Running(manifest, spec, stop, thread)
            if key not in self.started:
                self.started.append(key)
            logger.info("started %s", key)

    def _run_services_once(self) -> None:
        """Each service once, inline: a pre-set stop event makes a loop tick once and return."""
        done = threading.Event()
        done.set()
        for manifest, spec, interval in self._wanted_services():
            self._run_service(manifest, spec, interval, done)
            self.started.append(f"{manifest.name}:{spec.name}")
            if spec.interval_setting is not None:
                self.ticks += 1

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

    def _restart_requested(self) -> bool:
        from compass_core import extensions

        return extensions.self_restart() and extensions.restart_requested_after(self._started_at)

    def _job_worker(self) -> None:
        while not self._stop.is_set():
            if self._restart_requested():
                # checked before every claim: a job started right after a restart from the page
                # must wait for the restarted daemon, which knows the new set of captains
                logger.info("restart requested from the Captains page; exiting to load the new extensions")
                self.restarting = True
                self._stop.set()
                break
            try:
                self._run_queued_jobs()
            except Exception:  # noqa: BLE001
                logger.exception("job worker loop failed; backing off")
            self._stop.wait(self.job_poll)

    def _install_event_sink(self) -> None:
        """Job events go to the job in the jobs table as well as the log, so the dashboard
        follows a job's progress (a map scan's progress bar). Only over the open default:
        a host that installed its own sink keeps it."""
        from compass_core import events
        from compass_core.jobs import store_for

        if type(events.event_sink()) is not events.LogSink:
            return
        store = store_for(self.config)
        if store is not None:
            events.set_event_sink(events.StoreSink(store))

    # ── updates ─────────────────────────────────────────────────────────────

    def _update_tick(self) -> None:
        """Every check: status.json for the updater, the policy mirror, the release check
        (one fetch every few hours), one log line when a release appears, and the
        request when the policy lets the updater apply it on its own."""
        from compass_core import extensions, updates

        if extensions.directory() is None:
            return
        try:
            config = workspace_config(self.workspace_id)
        except LookupError:
            config = self.config
        updates.write_status(updates.count_active_jobs(config, self.workspace_id), by="serve")
        updates.mirror_policy(config)
        state = updates.check(config=config)
        if state.available and state.latest != self._announced_update:
            self._announced_update = state.latest
            logger.info("compass %s is available (this is %s): %s", state.latest, state.running, state.release.get("notes_url") if state.release else "")
        requested, why = updates.auto_request(config)
        if requested:
            logger.info("automatic update to %s requested (%s)", requested["target"], why)

    # ── run ─────────────────────────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        registry.load_all_commands()
        dispatch.use_inline_queue()
        self._install_event_sink()
        if self.map_port:
            self._start_map_viewer()
        if self.once:
            self._run_services_once()
            self.stop()
            return self._result()
        self._reconcile_services()
        if self.job_poll:
            worker = threading.Thread(target=self._job_worker, name="jobs", daemon=True)
            worker.start()
            self._threads.append(worker)
            logger.info("running jobs queued by the API (every %ss)", self.job_poll)
        logger.info("compass serving workspace %s: %s", self.workspace_id, ", ".join(self._services) or "no services")

        self._started_at = time.time()
        while not self._stop.wait(self.captains_poll):
            if self._restart_requested():
                # the Captains page installed or removed an extension: exit, and the
                # container's restart policy brings serve back with the new set
                logger.info("restart requested from the Captains page; exiting to load the new extensions")
                self.restarting = True
                break
            try:
                self._reconcile_services()
            except Exception:  # noqa: BLE001
                logger.exception("service check failed")
            try:
                self._update_tick()
            except Exception:  # noqa: BLE001
                logger.exception("update check failed")
        self.stop()
        for running in list(self._services.values()):
            running.thread.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        return self._result()

    def _result(self) -> Dict[str, Any]:
        return {"workspace_id": self.workspace_id, "ticks": self.ticks, "services": list(self.started), "restarting": self.restarting}


def serve(payload: Dict[str, Any]) -> Dict[str, Any]:
    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    sessions.assert_runtime(config)  # a profile in the container is a setup mistake; say so before anything runs
    intervals: Dict[str, int] = {}
    for manifest in registry.manifests():
        for spec in manifest.services:
            if spec.interval_setting and spec.interval_setting not in intervals:
                value = payload.get(spec.interval_setting, interval_from_env(spec.interval_setting))
                if value is None:
                    value = spec.interval
                intervals[spec.interval_setting] = int(value or 0)
    server = Server(
        workspace.workspace_id,
        config,
        prs_tick=intervals.pop("prs_tick", 300),
        map_interval=intervals.pop("map_interval", 0),
        once=payload.get("once") in (True, "true", "1", 1),
        map_port=int(payload.get("map_port", os.getenv("COMPASS_MAP_PORT", 0)) or 0),
        map_dir=str(payload.get("map_dir") or os.getenv("COMPASS_MAP_DIR") or "map"),
        job_poll=float(payload.get("job_poll", os.getenv("COMPASS_JOB_POLL_SECONDS", 2.0)) or 0),
        intervals=intervals,
        captains_poll=float(payload.get("captains_poll", os.getenv("COMPASS_CAPTAINS_POLL_SECONDS", 30)) or 30),
    )
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, server.stop)
        signal.signal(signal.SIGTERM, server.stop)
    return server.run()
