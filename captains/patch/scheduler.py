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
from captains.patch import notify as _notify
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
         findings: Optional[FindingStore] = None, now_ms: Optional[int] = None, open_prs: bool = True, opener: Any = None, verifier: Any = None) -> Dict[str, Any]:
    """One match, then the pull requests for what the policy or a person decided: returns what
    changed and the summary the tab shows."""
    started = time.time()
    if advisories is None or findings is None:
        advisories, findings = stores(config, workspace_id)
    inventory = inventory if inventory is not None else _inventory.load(config, workspace_id=workspace_id)
    topology = _match.Topology.from_graph(inventory.nodes, inventory.edges)
    found = _match.match(inventory, advisories.list(), topology)
    changes = findings.sync(found, config, now_ms=now_ms)
    prs: Dict[str, Any] = {}
    verification: Dict[str, Any] = {}
    if open_prs:
        from captains.patch.pr import PullRequestOpener

        try:
            opener = opener or PullRequestOpener(config, workspace_id=workspace_id, findings=findings, advisories=advisories, model=_model(config), topology=topology)
            if getattr(opener, "topology", None) is None:
                opener.topology = topology
            prs = opener.open_all(now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 - the findings stand even when GitHub does not answer
            logger.warning("patch pull requests not opened: %s", exc)
            prs = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        try:
            verification = _verify_open_prs(config, workspace_id=workspace_id, findings=findings, topology=topology, opener=opener, now_ms=now_ms, verifier=verifier)
        except Exception as exc:  # noqa: BLE001
            logger.warning("patch verification failed: %s", exc)
            verification = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    for row in changes.get("deployed_rows") or []:
        _notify.emit(config, workspace_id, "patch.deployed", {"advisory_id": row.get("advisory_id"), "workload": row.get("node_name"), "repo": row.get("repo"),
                                                              "url": (row.get("pr") or {}).get("url"), "number": (row.get("pr") or {}).get("number")})
    rows = findings.list()
    report = {"at": now_ms or int(time.time() * 1000), "seconds": round(time.time() - started, 2), "inventory": inventory.summary(),
              "coverage": _match.coverage(inventory.nodes), "advisories": advisories.count(), "matched": len(found), "changes": changes, "prs": prs,
              "verification": verification, "summary": summary(rows)}
    findings.table.put_item(Item={"pk": findings.pk, "sk": patch_cursor_sort_key(LAST_TICK), "source": LAST_TICK, "value": report["at"],
                                  "report": _to_dynamo(report)})
    return report


def _verify_open_prs(config: Dict[str, Any], *, workspace_id: str, findings: FindingStore, topology: Any, opener: Any, now_ms: Optional[int], verifier: Any = None) -> Dict[str, Any]:
    """CI, the build and the canary for every open fix pull request; the grant proposed once when a
    canary is named and none exists (docs/specs/patching-captain.md §3.6)."""
    from captains.patch import verify as _verify
    from captains.patch.actions import CanaryDeploy
    from compass_core import grants
    from compass_core.actions import captain_of

    table, pk = findings.table, findings.pk
    rows = grants.list_grants(table, pk, captain="patch")
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    canaries = section.get("canaries") if isinstance(section.get("canaries"), dict) else {}
    proposed = None
    if canaries and not any(str(r.get("action_type")) == CanaryDeploy.action_type for r in rows) and captain_of(CanaryDeploy.action_type) == "patch":
        try:
            scope = _canary_scope(config, canaries, topology)
            proposed = grants.propose(table, pk, captain="patch", action_type=CanaryDeploy.action_type, scope=scope, mode="autopilot", risk_max=2, proposed_by="patch captain")
            rows = grants.list_grants(table, pk, captain="patch")
        except Exception as exc:  # noqa: BLE001 - the canary is then skipped and the finding says why
            logger.warning("canary grant not proposed: %s", exc)
    if verifier is not None:
        report = verifier(config=config, workspace_id=workspace_id, findings=findings, services=topology.services, grant_rows=rows, now_ms=now_ms)
    else:
        from compass_core import sessions

        registry = opener.registry() if opener is not None else None

        def session_factory(grant_row: Dict[str, Any]) -> Any:
            return sessions.target("act", config=config, captain="patch", action=grants.narrowing(grant_row, config))

        def comment(repo: str, number: int, text: str) -> None:
            opener._repo(repo).get_pull(number).create_issue_comment(text)

        report = _verify.verify_all(config=config, workspace_id=workspace_id, findings=findings, registry=registry, services=topology.services, grant_rows=rows,
                                    session_factory=session_factory, read_session=sessions.target("read", config=config, captain="patch"),
                                    comment=comment if opener is not None else None, set_watch=registry.set_watch if registry is not None else None, now_ms=now_ms)
    if proposed is not None:
        report["grant_proposed"] = proposed.get("action_id")
    return report


def _canary_scope(config: Dict[str, Any], canaries: Dict[str, Any], topology: Any) -> Dict[str, Any]:
    """The grant's scope from ``patch.canaries``: the staging services as ARNs (built from the production
    service's ARN on the map when the twin is named, not given as an ARN) and the task roles they run as."""
    from captains.patch.verify import _staging_service_arn
    from compass_core.aws import resolve_target_account_id, resolve_target_region

    region = str(resolve_target_region(config) or "us-east-1")
    account = str(resolve_target_account_id(config) or "")
    by_name: Dict[str, str] = {}
    for services in (topology.services or {}).values():
        for svc in services:
            by_name[str(svc.get("name"))] = str(svc.get("node_id"))
    arns = sorted({_staging_service_arn(twin, by_name.get(name, ""), region, account) for name, twin in canaries.items() if isinstance(twin, dict)})
    roles = sorted({str(r) for twin in canaries.values() if isinstance(twin, dict) for r in twin.get("task_role_arns") or []})
    return {"service_arns": arns, "task_role_arns": roles}


def _model(config: Dict[str, Any]) -> Any:
    try:
        from compass_core.llm import model_for

        return model_for(config)
    except Exception:  # noqa: BLE001 - no model is the open default
        return None


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
