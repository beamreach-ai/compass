"""The ``patch-tick`` service: every fifteen minutes, the inventory from the map, the advisories
from the store, the match, the policy, the findings' rows brought up to date
(docs/specs/patching-captain.md §3.3, §3.4). The fix pull requests pick up from the rows this
leaves: ``open`` with verdict ``pr``, and ``approved``."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from captains.patch import inventory as _inventory
from captains.patch import match as _match
from captains.patch.findings import FindingStore, summary
from captains.patch.store import AdvisoryStore, _to_dynamo, _to_native
from compass_core.keys import patch_cursor_sort_key, workspace_pk
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("patch.scheduler")

LAST_TICK = "tick:last_run"


def stores(config: Dict[str, Any], workspace_id: str) -> tuple:
    name = tables(config).items
    if not name:
        raise ValueError("dynamodb_config.items is required: the findings live in the items table")
    table = dynamo_table(name)
    pk = workspace_pk(workspace_id)
    return AdvisoryStore(table, pk), FindingStore(table, pk)


def tick(config: Dict[str, Any], *, workspace_id: str, inventory: Any = None, advisories: Optional[AdvisoryStore] = None,
         findings: Optional[FindingStore] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """One match: returns what changed and the summary the tab shows."""
    started = time.time()
    if advisories is None or findings is None:
        advisories, findings = stores(config, workspace_id)
    inventory = inventory if inventory is not None else _inventory.load(config, workspace_id=workspace_id)
    topology = _match.Topology.from_graph(inventory.nodes, inventory.edges)
    found = _match.match(inventory, advisories.list(), topology)
    changes = findings.sync(found, config, now_ms=now_ms)
    rows = findings.list()
    report = {"at": now_ms or int(time.time() * 1000), "seconds": round(time.time() - started, 2), "inventory": inventory.summary(),
              "coverage": _match.coverage(inventory.nodes), "advisories": advisories.count(), "matched": len(found), "changes": changes, "summary": summary(rows)}
    findings.table.put_item(Item={"pk": findings.pk, "sk": patch_cursor_sort_key(LAST_TICK), "source": LAST_TICK, "value": report["at"],
                                  "report": _to_dynamo(report)})
    return report


def last_tick(config: Dict[str, Any], *, workspace_id: str, findings: Optional[FindingStore] = None) -> Optional[Dict[str, Any]]:
    if findings is None:
        _, findings = stores(config, workspace_id)
    row = findings.table.get_item(Key={"pk": findings.pk, "sk": patch_cursor_sort_key(LAST_TICK)}).get("Item")
    return _to_native(dict(row.get("report") or {})) if row else None


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """A `compass serve` service: the match every fifteen minutes (``patch_tick`` in the serve payload)."""
    wait = float(interval or 900)
    while True:
        try:
            report = tick(config, workspace_id=workspace_id)
            logger.info("patch tick: %d findings open, %d exposed, %d opened, %d closed", report["summary"]["open"], report["summary"]["exposed"],
                        report["changes"]["opened"], report["changes"]["closed"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("patch tick failed: %s", exc)
        if stop.wait(wait):
            return
