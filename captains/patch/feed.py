"""The ``cve-feed`` service: every hour, each source in turn, what changed into the advisory
store, the cursors advanced, the run recorded (docs/specs/patching-captain.md §3.2).

A run reads the inventory from the map first, so OSV is asked about what the account
actually runs and the trackers about the distros it uses. Each source is independent:
one that fails is logged under its name and the others run; the Patches tab shows each
source's last success. ``inject`` puts an advisory in the store by hand, for the e2e
suite and the demo replay, behind ``COMPASS_PATCH_INJECT=1``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests

from captains.patch import inventory as _inventory
from captains.patch import sources as _sources
from captains.patch.advisories import Advisory
from captains.patch.sources import parse_osv
from captains.patch.store import AdvisoryStore
from compass_core.keys import workspace_pk
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("patch.feed")

USER_AGENT = "compass-patch (+https://github.com/beamreach-ai/compass)"
TIMEOUT = 60   # ubuntu.com takes its time
LAST_RUN = "feed:last_run"


class Http:
    """The feeds' client: JSON and text over GET and POST, with a timeout and the user agent
    the public feeds ask for. ``allow_404`` turns a missing page into None."""

    def __init__(self, session: Optional[requests.Session] = None, *, timeout: int = TIMEOUT):
        self.session = session or requests.Session()
        if session is None:
            from requests.adapters import HTTPAdapter

            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=_sources.WORKERS)   # the record fetches run on that many threads
            self.session.mount("https://", adapter)
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.timeout = timeout
        self.calls = 0

    def _get(self, url: str, params: Optional[Dict[str, str]], allow_404: bool) -> Optional[requests.Response]:
        self.calls += 1
        response = self.session.get(url, params=params, timeout=self.timeout)
        if allow_404 and response.status_code == 404:
            return None
        response.raise_for_status()
        return response

    def get(self, url: str, params: Optional[Dict[str, str]] = None, *, allow_404: bool = False) -> Optional[str]:
        response = self._get(url, params, allow_404)
        return response.text if response is not None else None

    def get_json(self, url: str, params: Optional[Dict[str, str]] = None, *, allow_404: bool = False) -> Any:
        response = self._get(url, params, allow_404)
        return response.json() if response is not None else None

    def post_json(self, url: str, body: Any) -> Any:
        self.calls += 1
        response = self.session.post(url, json=body, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


def _store(config: Dict[str, Any], workspace_id: str) -> AdvisoryStore:
    name = tables(config).items
    if not name:
        raise ValueError("dynamodb_config.items is required: the advisories live in the items table")
    return AdvisoryStore(dynamo_table(name), workspace_pk(workspace_id))


def run(config: Dict[str, Any], *, workspace_id: str, http: Any = None, now: Optional[datetime] = None, inventory: Any = None,
        sources: Optional[Iterable[str]] = None, store: Optional[AdvisoryStore] = None) -> Dict[str, Any]:
    """One pass over the feeds. Returns the report the ``feed_status`` command shows."""
    now = now or datetime.now(timezone.utc)
    store = store or _store(config, workspace_id)
    http = http or Http()
    if inventory is None:
        try:
            inventory = _inventory.load(config, workspace_id=workspace_id)
        except Exception as exc:  # noqa: BLE001 - a map that cannot be read leaves the feeds to KEV and the stored ids
            logger.warning("inventory not read: %s", exc)
            inventory = _inventory.Inventory()
    stored_ids = set(store.all_ids())
    kev_row = store.cursor("kev") or {}
    ctx = _sources.Context(http=http, now=now, inventory=inventory, stored_ids=stored_ids, kev_ids=set(kev_row.get("ids") or []),
                           cursors=store.cursors(), lookup=store.get)
    report: Dict[str, Any] = {"started_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "inventory": inventory.summary(), "sources": {}}
    for name, fetch in _sources.by_name(sources):
        started = time.time()
        entry: Dict[str, Any] = {"fetched": 0, "stored": 0}
        try:
            result = fetch(ctx)
            stored = 0
            for advisory in result.advisories:
                merged = store.put(advisory)
                stored += 1
                ctx.stored_ids.update(merged.all_ids)
            fields = {"last_success": report["started_at"], **({"ids": result.extra["ids"]} if name == "kev" and result.extra.get("ids") else {})}
            if result.cursor is not None:
                store.set_cursor(name, result.cursor, **fields)
                if name == "kev":
                    ctx.kev_ids = set(result.extra.get("ids") or ctx.kev_ids)
            entry.update(fetched=result.fetched, stored=stored, cursor=result.cursor, note=result.note, **{k: v for k, v in result.extra.items() if k != "ids"})
            ctx.cursors = store.cursors()
        except Exception as exc:  # noqa: BLE001 - one source never stops the others
            logger.warning("feed %s failed: %s", name, exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        entry["seconds"] = round(time.time() - started, 2)
        report["sources"][name] = entry
    report["advisories"] = store.count()
    report["finished_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report["http_calls"] = getattr(http, "calls", None)
    store.set_cursor(LAST_RUN, report["finished_at"], report={k: v for k, v in report.items() if k != "inventory"}, inventory=report["inventory"])
    return report


def status(config: Dict[str, Any], *, workspace_id: str, store: Optional[AdvisoryStore] = None) -> Dict[str, Any]:
    store = store or _store(config, workspace_id)
    cursors = store.cursors()
    last = cursors.pop(LAST_RUN, None) or {}
    return {"advisories": store.count(), "last_run": last.get("value"), "report": last.get("report"), "inventory": last.get("inventory"),
            "sources": {name: {"cursor": row.get("value"), "last_success": row.get("last_success"), "updated_at": row.get("updated_at")}
                        for name, row in cursors.items() if name in _sources.names()}}


def inject(config: Dict[str, Any], *, workspace_id: str, advisory: Dict[str, Any], store: Optional[AdvisoryStore] = None) -> Dict[str, Any]:
    """Put one advisory in the store as if a feed had: an OSV record or this module's own shape.
    A test-only door, open when ``COMPASS_PATCH_INJECT=1``."""
    if os.getenv("COMPASS_PATCH_INJECT") != "1":
        raise ValueError("feed inject is a test-only command: set COMPASS_PATCH_INJECT=1 to allow it")
    store = store or _store(config, workspace_id)
    parsed = parse_osv(advisory) if "affected" in advisory and any(isinstance(a, dict) and "package" in a for a in advisory.get("affected") or []) else Advisory.from_dict(advisory)
    parsed.sources = dict(parsed.sources) or {"inject": datetime.now(timezone.utc).isoformat()}
    merged = store.put(parsed)
    return {"id": merged.id, "aliases": merged.aliases, "affected": len(merged.affected), "cvss": merged.cvss, "kev": bool(merged.kev)}


def advisories(config: Dict[str, Any], *, workspace_id: str, limit: int = 200, store: Optional[AdvisoryStore] = None) -> List[Dict[str, Any]]:
    store = store or _store(config, workspace_id)
    return [a.to_dict() for a in store.list(limit=limit)]


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """A `compass serve` service: the feeds every hour (``cve_feed`` in the serve payload)."""
    wait = float(interval or 3600)
    while True:
        try:
            report = run(config, workspace_id=workspace_id)
            failed = [n for n, s in report["sources"].items() if s.get("error")]
            logger.info("cve feed: %d advisories stored, %s", report["advisories"], f"failed: {', '.join(failed)}" if failed else "every source ok")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cve feed failed: %s", exc)
        if stop.wait(wait):
            return
