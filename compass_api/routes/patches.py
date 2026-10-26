"""``/api/patches``: the findings by workload and repository with their verdict and state, the
feed's and the match's last runs, the inventory's coverage; the writes a person makes
(approve, ignore) and the two jobs a person can start (the feeds now, the match now)
(docs/specs/patching-captain.md §3.8). The fix pull requests' routes join here with them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from captains.patch import feed as _feed
from captains.patch import findings as _findings
from captains.patch import scheduler as _scheduler
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, get_workspace_dynamodb_config
from compass_core.keys import workspace_pk
from compass_core.queue import queue_proxy
from compass_core.tables import dynamo_table, tables

router = APIRouter()
logger = logging.getLogger("compass.api.patches")
_queue_client = queue_proxy


def _stores(request: Request) -> tuple:
    name = tables(get_workspace_dynamodb_config(request)).items
    if not name:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    table = dynamo_table(name)
    pk = workspace_pk(get_actor_ids(request).get("workspace_id"))
    from captains.patch.store import AdvisoryStore

    return AdvisoryStore(table, pk), _findings.FindingStore(table, pk)


def _actor(request: Request) -> str:
    actor = get_actor_ids(request)
    return str(actor.get("user_id") or actor.get("email") or "user")


def _invalidate() -> None:
    from compass_core import inbox

    inbox.invalidate()


@router.get("/api/patches")
def get_patches(request: Request, state: Optional[str] = None, exposed: Optional[bool] = None, exploited: Optional[bool] = None,
                repo: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
    """Everything the Patches tab shows in one call."""
    config = get_workspace_config(request)
    workspace_id = get_actor_ids(request).get("workspace_id")
    advisories, findings = _stores(request)
    rows = findings.list()
    chosen = [r for r in rows if (not state or r.get("state") == state) and (exposed is None or bool(r.get("exposed")) == exposed)
              and (exploited is None or bool(r.get("kev")) == exploited) and (not repo or r.get("repo") == repo)]
    if state is None:
        chosen = [r for r in chosen if r.get("state") in _findings.OPEN_STATES]
    last_tick = _scheduler.last_tick(config, workspace_id=str(workspace_id), findings=findings)
    feed_status = _feed.status(config, workspace_id=str(workspace_id), store=advisories)
    return {
        "generated_at": int(time.time() * 1000),
        "summary": _findings.summary(rows),
        "findings": chosen[:limit],
        "truncated": len(chosen) > limit,
        "feed": feed_status,
        "tick": last_tick,
        "coverage": (last_tick or {}).get("coverage"),
        "settings": {**_settings(config)},
    }


def _settings(config: Dict[str, Any]) -> Dict[str, Any]:
    from captains.patch.policy import settings

    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    return {"act_on": settings(config), "repos": section.get("repos") or [], "max_open_prs": section.get("max_open_prs", 10), "ignore": section.get("ignore") or []}


@router.get("/api/patches/findings")
def list_findings(request: Request, state: Optional[str] = None, exposed: Optional[bool] = None, exploited: Optional[bool] = None, repo: Optional[str] = None) -> List[Dict[str, Any]]:
    return get_patches(request, state=state, exposed=exposed, exploited=exploited, repo=repo)["findings"]


@router.get("/api/patches/findings/{key}")
def get_finding(request: Request, key: str) -> Dict[str, Any]:
    advisories, findings = _stores(request)
    row = findings.get(key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"finding {key} not found")
    advisory = advisories.get(str(row.get("advisory_id") or ""))
    return {"finding": row, "advisory": advisory.to_dict() if advisory else None}


class KeysPayload(BaseModel):
    keys: List[str] = Field(default_factory=list)
    repo: Optional[str] = None


class IgnorePayload(KeysPayload):
    until: Optional[str] = None
    days: Optional[int] = Field(default=None, ge=1, le=365)
    reason: Optional[str] = None


def _targets(findings: _findings.FindingStore, payload: KeysPayload) -> List[Dict[str, Any]]:
    rows = findings.list()
    if payload.keys:
        wanted = set(payload.keys)
        return [r for r in rows if str(r.get("key")) in wanted]
    if payload.repo:
        return [r for r in rows if r.get("repo") == payload.repo and r.get("state") == "open"]
    raise HTTPException(status_code=400, detail="keys or repo is required")


@router.post("/api/patches/findings/approve")
def approve_findings(request: Request, payload: KeysPayload) -> Dict[str, Any]:
    """A person's yes to the inbox item: the findings become ``approved`` and the next tick opens the pull request."""
    _, findings = _stores(request)
    by = _actor(request)
    done = [findings.approve(str(r["key"]), by=by) for r in _targets(findings, payload)]
    _invalidate()
    return {"approved": sum(1 for r in done if r and r.get("state") == "approved"), "keys": [str(r["key"]) for r in done if r]}


@router.post("/api/patches/findings/ignore")
def ignore_findings(request: Request, payload: IgnorePayload) -> Dict[str, Any]:
    _, findings = _stores(request)
    until = payload.until or (datetime.now(timezone.utc) + timedelta(days=payload.days or 30)).date().isoformat()
    by = _actor(request)
    done = [findings.ignore(str(r["key"]), by=by, until=until, reason=payload.reason) for r in _targets(findings, payload)]
    _invalidate()
    return {"ignored": sum(1 for r in done if r and r.get("state") == "ignored"), "until": until, "keys": [str(r["key"]) for r in done if r]}


@router.post("/api/patches/findings/{key}/ignore")
def ignore_finding(request: Request, key: str, payload: IgnorePayload) -> Dict[str, Any]:
    return ignore_findings(request, IgnorePayload(keys=[key], until=payload.until, days=payload.days, reason=payload.reason))


@router.post("/api/patches/findings/{key}/reopen")
def reopen_finding(request: Request, key: str) -> Dict[str, Any]:
    _, findings = _stores(request)
    row = findings.get(key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"finding {key} not found")
    if row.get("state") != "ignored":
        raise HTTPException(status_code=409, detail=f"finding is {row.get('state')}, not ignored")
    updated = findings.update(key, state="open", ignored=None)
    _invalidate()
    return updated or {}


def _enqueue(request: Request, command: str, trigger: str) -> Dict[str, Any]:
    workspace_id = get_actor_ids(request).get("workspace_id")
    job = _queue_client.enqueue(captain="patch", command=command, payload={"workspace_id": workspace_id, "trigger": trigger},
                                session_id=f"patch-{command}", enforce_single_active_session=True)
    return {"job_id": job.get("job_id"), "status": job.get("status"), "command": command}


@router.post("/api/patches/feed/run", status_code=202)
def run_feed(request: Request) -> Dict[str, Any]:
    """The feeds now: what a person does the day a disclosure lands."""
    return _enqueue(request, "feed_run", "api")


@router.post("/api/patches/tick", status_code=202)
def run_tick(request: Request) -> Dict[str, Any]:
    """The match now, and the pull requests it decides."""
    return _enqueue(request, "tick", "api")


class RepoPayload(BaseModel):
    repo: str


@router.post("/api/patches/pr", status_code=202)
def open_pr(request: Request, payload: RepoPayload) -> Dict[str, Any]:
    """The fix pull request of one repository, now, as a job."""
    workspace_id = get_actor_ids(request).get("workspace_id")
    job = _queue_client.enqueue(captain="patch", command="pr", payload={"workspace_id": workspace_id, "repo": payload.repo, "trigger": "api"},
                                session_id=f"patch-pr-{payload.repo}", enforce_single_active_session=True)
    return {"job_id": job.get("job_id"), "status": job.get("status"), "command": "pr", "repo": payload.repo}
