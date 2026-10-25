"""``GET /api/status``: one call that tells an agent or the CLI where this install stands.

Each section is computed independently; a failing section is reported under
``errors`` instead of failing the whole call, so ``compass status`` still works
on a half-configured install.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Request

from compass_api import extensions
from compass_api.auth.tokens import bootstrap_configured
from compass_api.routes._app_config import get_app_config
from compass_api.routes._infra_scan import current_infra_scan_summary
from compass_api.routes._workspace import get_actor_ids, get_workspace_config
from compass_api.routes.auth import _auth_enabled
from compass_api.version import API_VERSION, compass_version
from compass_core import updates as _updates
from compass_core.queue import queue_proxy

router = APIRouter()
logger = logging.getLogger(__name__)
_queue_client = queue_proxy

_ACTIVE_STATUSES = {"queued", "running", "retrying"}
_SEVERITIES = ("critical", "high", "medium", "low")


def _section(errors: Dict[str, str], name: str, compute: Callable[[], Any], default: Any = None) -> Any:
    try:
        return compute()
    except Exception as exc:  # noqa: BLE001 - status must degrade, not fail
        logger.warning("status section %s failed: %s", name, exc)
        errors[name] = str(exc)
        return default


def _auth_section(request: Request) -> Dict[str, Any]:
    principal = getattr(request.state, "api_principal", None)
    return {
        "method": "api_token" if principal is not None else ("session" if _auth_enabled() else "none"),
        "scope": principal.scope if principal is not None else None,
        "token_kind": principal.kind if principal is not None else None,
        "bootstrap_configured": bootstrap_configured(),
    }


def _target_section(request: Request, config: Dict[str, Any]) -> Dict[str, Any]:
    from compass_api.routes.settings import _aws_summary, _github_summary

    aws = _aws_summary(config)
    github = _github_summary(config)
    return {
        "aws": aws,
        "github": github,
        "aws_connected": bool(aws.get("configured") or aws.get("mode")),
        "github_connected": bool(github.get("connected") or github.get("installation_id")),
    }


def _access_section(request: Request, config: Dict[str, Any]) -> Dict[str, Any]:
    """What `compass status` prints on its account line: the roles, how many actions are
    armed, when the access was last verified and audited (compass_core.grants, access_audit)."""
    from compass_api.routes._workspace import get_workspace_dynamodb_config
    from compass_core import grants
    from compass_core.aws import roles
    from compass_core.keys import workspace_pk
    from compass_core.tables import dynamo_table, tables

    aws = config.get("aws") if isinstance(config.get("aws"), dict) else {}
    out: Dict[str, Any] = {"connected": bool(roles(config).get("read")), "roles": roles(config), "verified_at": aws.get("verified_at"),
                           "actions_total": 0, "actions_armed": 0, "in_flight": 0, "waiting_on": [], "audited_at": None, "findings": 0}
    name = tables(get_workspace_dynamodb_config(request)).items
    if not name:
        return out
    table = dynamo_table(name)
    pk = workspace_pk(get_actor_ids(request).get("workspace_id"))
    summary = grants.summary(table, pk)
    out.update(actions_total=summary["total"], actions_armed=summary["armed"], in_flight=summary["in_flight"], waiting_on=summary["waiting_on"])
    try:
        from compass_core import access_audit

        last = access_audit.last_run(table, pk)
        out["audited_at"] = (last or {}).get("at")
        out["findings"] = len(access_audit.open_findings(table, pk))
    except ImportError:
        pass
    return out


def _jobs_section(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    active = [j for j in jobs if str(j.get("status") or "").lower() in _ACTIVE_STATUSES]
    last_completed: Dict[str, Optional[int]] = {}
    for job in jobs:
        if str(job.get("status") or "").lower() != "completed":
            continue
        key = f"{job.get('captain')}.{job.get('command')}"
        ts = job.get("updated_at") or job.get("created_at")
        if ts and (last_completed.get(key) is None or ts > last_completed[key]):
            last_completed[key] = ts
    failed_recent = [
        {"job_id": j.get("job_id"), "captain": j.get("captain"), "command": j.get("command"), "error": j.get("error")}
        for j in jobs
        if str(j.get("status") or "").lower() in {"failed", "error"}
    ][:5]
    return {
        "active": [
            {"job_id": j.get("job_id"), "captain": j.get("captain"), "command": j.get("command"), "status": j.get("status")}
            for j in active
        ],
        "active_count": len(active),
        "last_completed": last_completed,
        "recent_failures": failed_recent,
    }


def _prs_section(jobs: List[Dict[str, Any]], records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    from compass_api.routes.prs import collect_prs

    prs = collect_prs(jobs)
    with_url = [p for p in prs if p.get("pr_url")]
    pending = [p for p in prs if not p.get("pr_url") and str(p.get("job_status") or "") in _ACTIVE_STATUSES]
    records = records or []
    open_records = [r for r in records if str(r.get("state") or "open") == "open"]
    return {
        "opened": len(with_url),
        "pending": len(pending),
        "tracked": len(records),
        "open": len(open_records),
        "needs_human": sum(1 for r in open_records if str((r.get("watch") or {}).get("status") or "") == "needs_human"),
        "pending_approval": sum(1 for r in open_records if r.get("pending_revision")),
        "ready": sum(1 for r in open_records if (r.get("github") or {}).get("ready")),
        "merged": sum(1 for r in records if r.get("state") == "merged"),
        "recent": with_url[:10],
    }


def _pr_records(config: Dict[str, Any], workspace_id: Optional[str]) -> List[Dict[str, Any]]:
    from compass_core.registry import PRRegistry

    return PRRegistry.from_config(config, workspace_id=workspace_id).list(limit=500)


def _honeypots_section(request: Request) -> Dict[str, Any]:
    from compass_api.routes.honeypots import list_honeytokens

    tokens = list_honeytokens(request)
    by_status: Dict[str, int] = {}
    active_alerts = 0
    for token in tokens:
        status = str(token.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        active_alerts += int(token.get("active_alert_count") or 0)
    return {"total": len(tokens), "by_status": by_status, "active_alerts": active_alerts}


def _patches_section(request: Request, config: Dict[str, Any], workspace_id: Optional[str]) -> Dict[str, Any]:
    """What `compass status` prints on its Patches line: the findings' counts and when the feeds last ran."""
    from compass_api.routes._workspace import get_workspace_dynamodb_config
    from compass_core.keys import workspace_pk
    from compass_core.tables import dynamo_table, tables

    name = tables(get_workspace_dynamodb_config(request)).items
    if not name:
        return {}
    from captains.patch.findings import FindingStore, summary
    from captains.patch.store import AdvisoryStore

    table = dynamo_table(name)
    pk = workspace_pk(workspace_id)
    counts = summary(FindingStore(table, pk).list())
    advisories = AdvisoryStore(table, pk)
    last = advisories.cursor("feed:last_run") or {}
    return {**{k: counts.get(k, 0) for k in ("open", "exposed", "exploited", "pr_now", "waiting_approval", "pr_open", "merged", "deployed", "ignored")},
            "advisories": advisories.count(), "feed_last_run": last.get("value")}


def _inbox_section(request: Request, config: Dict[str, Any], workspace_id: Optional[str]) -> Dict[str, Any]:
    """What `compass status` prints on its "Needs you" line: the counts and the three newest items."""
    from compass_api.routes.inbox import context_for
    from compass_core import inbox

    feed = inbox.items(context_for(request, config=config, workspace_id=workspace_id))
    return {"counts": feed["counts"], "line": inbox.status_line(feed["counts"]),
            "newest": [{"id": i["id"], "kind": i["kind"], "verb": i["verb"], "severity": i["severity"], "title": i["title"]} for i in feed["items"][:3]]}


@router.get("/api/status")
def get_status(request: Request) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    actor_ids = get_actor_ids(request)
    config = _section(errors, "config", lambda: get_workspace_config(request), default={})
    app_config = get_app_config()
    workspace_id = actor_ids.get("workspace_id")

    jobs: List[Dict[str, Any]] = _section(
        errors, "jobs", lambda: _queue_client.list_jobs(limit=200, workspace_id=workspace_id), default=[]
    )

    return {
        "api_version": API_VERSION,
        "compass_version": compass_version(),
        "update": _section(errors, "update", lambda: _updates.check(config=config).to_dict(), default={}),
        "generated_at": int(time.time() * 1000),
        "workspace": {"workspace_id": workspace_id, "org_id": actor_ids.get("org_id"), "user_id": actor_ids.get("user_id")},
        "app": {
            "mode": app_config.get("mode"),
            "read_only": app_config.get("read_only"),
            "settings_enabled": app_config.get("settings_enabled"),
            "disabled_features": app_config.get("disabled_features"),
        },
        "auth": _auth_section(request),
        "target": _section(errors, "target", lambda: _target_section(request, config), default={}),
        "access": _section(errors, "access", lambda: _access_section(request, config), default={}),
        "infra_scan": _section(errors, "infra_scan", lambda: current_infra_scan_summary(request, config=config), default={}),
        "jobs": _section(errors, "jobs_summary", lambda: _jobs_section(jobs), default={}),
        "prs": _section(errors, "prs", lambda: _prs_section(jobs, _section(errors, "pr_registry", lambda: _pr_records(config, workspace_id), default=[])), default={}),
        "honeypots": _section(errors, "honeypots", lambda: _honeypots_section(request), default={}),
        "inbox": _section(errors, "inbox", lambda: _inbox_section(request, config, workspace_id), default={}),
        "patches": _section(errors, "patches", lambda: _patches_section(request, config, workspace_id), default={}),
        # billing, findings: the product's sections (compass_api.extensions); absent in the open edition
        **{name: _section(errors, name, lambda fn=fn: fn(request, config, workspace_id), default={}) for name, fn in extensions.status_sections().items()},
        "errors": errors,
    }
