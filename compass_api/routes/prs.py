"""``GET /api/prs``: every pull request Compass opened, across captains.

Sources, in order of reliability:
* iac import / fix_drift: ``result_summary.pr_url`` / ``pr_urls``
* honeypot plant / decommission / responder_pr: ``result_summary.pr_url``
* engine / finops_engine solve: PR URLs appear in job events (``data.pr_url`` or
  ``data.pr_urls``) and in the progress log, not in the result summary.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Query, Request

from compass_api.routes._workspace import get_actor_ids
from compass_core.queue import queue_proxy

router = APIRouter()
_queue_client = queue_proxy

_PR_URL_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

_TYPE_LABELS = {
    ("iac", "import"): "iac_import",
    ("iac", "fix_drift"): "fix_drift",
    ("engine", "solve"): "security_fix",
    ("finops_engine", "solve"): "finops_fix",
    ("honeypot", "plant"): "honeypot_plant",
    ("honeypot", "decommission"): "honeypot_decommission",
    ("honeypot", "responder_pr"): "honeypot_responder",
}


def _repo_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.match(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/\d+", url)
    return match.group(1) if match else None


def _urls_from_events(job: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for event in job.get("events") or []:
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict):
            if data.get("pr_url"):
                urls.append(str(data["pr_url"]))
            for url in data.get("pr_urls") or []:
                urls.append(str(url))
        message = (event or {}).get("message") if isinstance(event, dict) else None
        if isinstance(message, str):
            urls.extend(_PR_URL_RE.findall(message))
    seen: List[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    return seen


def _base(job: Dict[str, Any], pr_type: str) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    return {
        "job_id": job.get("job_id"),
        "captain": job.get("captain"),
        "command": job.get("command"),
        "type": pr_type,
        "job_status": job.get("status"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "error": job.get("error"),
        "finding_id": payload.get("finding_id"),
        "resource_count": None,
        "repo": None,
        "pr_url": None,
    }


def prs_from_job(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    captain = str(job.get("captain") or "")
    command = str(job.get("command") or "")
    pr_type = _TYPE_LABELS.get((captain, command))
    if not pr_type:
        return []
    result = job.get("result_summary") or {}
    payload = job.get("payload") or {}
    entries: List[Dict[str, Any]] = []

    if captain == "iac":
        if command == "import":
            entry = _base(job, pr_type)
            entry["repo"] = payload.get("repo")
            entry["pr_url"] = result.get("pr_url")
            entry["resource_count"] = result.get("resource_count") or len(payload.get("resource_ids") or [])
            if entry["pr_url"] or job.get("status") in ("failed", "running", "queued", "retrying"):
                entries.append(entry)
        else:
            pr_urls = result.get("pr_urls") or []
            repos = result.get("repos") or []
            if pr_urls:
                for index, url in enumerate(pr_urls):
                    entry = _base(job, pr_type)
                    entry["pr_url"] = url
                    entry["repo"] = repos[index] if index < len(repos) else _repo_from_url(url)
                    entry["resource_count"] = result.get("resource_count")
                    entries.append(entry)
            else:
                entry = _base(job, pr_type)
                entry["resource_count"] = len(payload.get("resource_ids") or [])
                entries.append(entry)
        return entries

    urls: List[str] = []
    if result.get("pr_url"):
        urls.append(str(result["pr_url"]))
    for url in result.get("pr_urls") or []:
        if str(url) not in urls:
            urls.append(str(url))
    for url in _urls_from_events(job):
        if url not in urls:
            urls.append(url)

    if urls:
        for url in urls:
            entry = _base(job, pr_type)
            entry["pr_url"] = url
            entry["repo"] = _repo_from_url(url) or payload.get("repo")
            entries.append(entry)
    elif job.get("status") in ("failed", "running", "queued", "retrying"):
        entry = _base(job, pr_type)
        entry["repo"] = payload.get("repo")
        entries.append(entry)
    return entries


def collect_prs(jobs: Iterable[Dict[str, Any]], *, captains: Optional[set] = None) -> List[Dict[str, Any]]:
    prs: List[Dict[str, Any]] = []
    for job in jobs:
        if captains and job.get("captain") not in captains:
            continue
        prs.extend(prs_from_job(job))
    prs.sort(key=lambda p: p.get("created_at") or 0, reverse=True)
    return prs


@router.get("/api/prs")
def list_prs(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    captain: Optional[str] = Query(default=None, description="iac | engine | finops_engine | honeypot"),
    state: Optional[str] = Query(default=None, description="open | merged | closed (tracked PRs only)"),
    needs_human: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Registry records first (``tracked: true``), then PRs only known from job records."""
    actor_ids = get_actor_ids(request)
    jobs = _queue_client.list_jobs(limit=limit, workspace_id=actor_ids.get("workspace_id"))
    captains = {captain} if captain else None
    job_rows = collect_prs(jobs, captains=captains)
    try:
        records = _registry(request).list(state=state, limit=limit)
    except Exception:
        records = []
    if captains:
        records = [r for r in records if str((r.get("origin") or {}).get("captain") or "") in captains]
    if needs_human:
        records = [r for r in records if str((r.get("watch") or {}).get("status") or "") == "needs_human"]
        job_rows = []
    if state:
        job_rows = []
    return _merge_with_jobs(records, job_rows)[:limit]


# ── Registry-backed PR records (docs/pr_watcher_spec.md §2, §6) ────────────


from fastapi import Body, HTTPException  # noqa: E402

from captains.prs import policy as _policy  # noqa: E402
from captains.prs.intent import describe_invariant  # noqa: E402
from compass_api.routes._app_config import ensure_not_read_only  # noqa: E402
from compass_api.routes._workspace import get_workspace_config, save_config  # noqa: E402
from compass_core.registry import PRRegistry  # noqa: E402


def _registry(request: Request) -> PRRegistry:
    config = get_workspace_config(request)
    actor = get_actor_ids(request)
    return PRRegistry.from_config(config, workspace_id=actor.get("workspace_id"))


def _public_record(record: Dict[str, Any], *, full: bool = False) -> Dict[str, Any]:
    out = dict(record)
    intent = dict(out.get("intent") or {})
    contract = intent.pop("contract", None)
    intent["invariants_text"] = [describe_invariant(i) for i in intent.get("invariants") or [] if isinstance(i, dict)]
    out["intent"] = intent
    out["tracked"] = True
    out["job_status"] = None
    out["pr_url"] = out.get("url")
    if not full:
        out.pop("revisions", None)
        out.pop("decisions", None)
        out["revision_count"] = int((record.get("counters") or {}).get("revisions") or 0)
        out["pending_revision"] = bool(record.get("pending_revision"))
    elif contract is not None:
        out["intent"]["has_contract"] = True
    return out


def _merge_with_jobs(records: List[Dict[str, Any]], job_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_url = {r.get("url"): r for r in records}
    merged: List[Dict[str, Any]] = [_public_record(r) for r in records]
    for row in job_rows:
        url = row.get("pr_url")
        if url and url in by_url:
            continue
        row = dict(row)
        row["tracked"] = False
        row["state"] = None
        row["watch"] = None
        merged.append(row)
    merged.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or 0, reverse=True)
    return merged


@router.get("/api/prs/watcher")
def get_watcher_settings(request: Request) -> Dict[str, Any]:
    return _policy.watcher_settings(get_workspace_config(request))


@router.put("/api/prs/watcher")
def put_watcher_settings(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    ensure_not_read_only()
    config = get_workspace_config(request)
    mode = payload.get("mode")
    if mode is not None and str(mode).lower() not in _policy.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(_policy.MODES)}")
    for key in ("risk_max", "max_revisions"):
        if key in payload and payload[key] is not None:
            try:
                payload[key] = int(payload[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be an integer")
    settings = _policy.apply_settings_update(config, payload)
    save_config(request, config)
    return settings


@router.get("/api/prs/{owner}/{repo}/{number}")
def get_pr(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    record = _registry(request).get(f"{owner}/{repo}", number)
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    return _public_record(record, full=True)


@router.get("/api/prs/{owner}/{repo}/{number}/context")
def get_pr_context(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    from captains.prs.context import load_origin_context

    record = _registry(request).get(f"{owner}/{repo}", number)
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    actor = get_actor_ids(request)
    config = dict(get_workspace_config(request))
    config.setdefault("org_id", actor.get("org_id") or actor.get("workspace_id"))
    return load_origin_context(record, config=config, workspace_id=str(actor.get("workspace_id") or ""))


def _enqueue_address(request: Request, repo: str, number: int, extra: Dict[str, Any]) -> Dict[str, Any]:
    actor = get_actor_ids(request)
    payload = {
        "repo": repo,
        "number": number,
        "workspace_id": actor.get("workspace_id"),
        "org_id": actor.get("org_id"),
        "user_id": actor.get("user_id"),
        **extra,
    }
    job = _queue_client.enqueue(captain="prs", command="address", payload=payload, session_id=f"pr:{repo}#{number}", enforce_single_active_session=True)
    return {"job_id": job.get("job_id"), "status": job.get("status")}


@router.post("/api/prs/sync")
def sync_all_prs(request: Request) -> Dict[str, Any]:
    """Refresh every open tracked PR from GitHub now instead of waiting for the ticker."""
    ensure_not_read_only()
    actor = get_actor_ids(request)
    job = _queue_client.enqueue(captain="prs", command="sync", payload={"workspace_id": actor.get("workspace_id"), "org_id": actor.get("org_id"), "user_id": actor.get("user_id"), "force": True, "trigger": "manual"}, session_id="prs-sync", enforce_single_active_session=True)
    return {"job_id": job.get("job_id"), "status": job.get("status")}


@router.post("/api/prs/{owner}/{repo}/{number}/sync")
def sync_pr(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    ensure_not_read_only()
    full_repo = f"{owner}/{repo}"
    if not _registry(request).get(full_repo, number):
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    actor = get_actor_ids(request)
    job = _queue_client.enqueue(captain="prs", command="sync", payload={"repo": full_repo, "number": number, "workspace_id": actor.get("workspace_id"), "org_id": actor.get("org_id"), "user_id": actor.get("user_id"), "force": True, "trigger": "manual"}, session_id=f"pr-sync:{full_repo}#{number}", enforce_single_active_session=True)
    return {"job_id": job.get("job_id"), "status": job.get("status")}


@router.post("/api/prs/{owner}/{repo}/{number}/address")
def address_pr(owner: str, repo: str, number: int, request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Revise the PR. Without ``confirm`` returns what would happen under the current policy."""
    ensure_not_read_only()
    full_repo = f"{owner}/{repo}"
    registry = _registry(request)
    record = registry.get(full_repo, number)
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    message = payload.get("message")
    fetch = bool(payload.get("fetch_comments", not message))
    settings = _policy.watcher_settings(get_workspace_config(request))
    feedback = {"comments": payload.get("comments") or [], "message": message}
    requested = _policy.requested_files_from_feedback(feedback)
    risk = _policy.revision_risk(record.get("intent") or {}, requested)
    verdict = _policy.decide(settings, risk=risk["risk"], record=record, approved=bool(payload.get("approved")), trigger="manual")
    plan = {
        "mode": settings["mode"],
        "risk_max": settings["risk_max"],
        "max_revisions": settings["max_revisions"],
        "revisions_so_far": int((record.get("counters") or {}).get("revisions") or 0),
        "risk": risk,
        "decision": verdict.get("decision"),
        "reason": verdict.get("reason"),
        "fetch_comments": fetch,
        "message": message,
    }
    if not payload.get("confirm", True):
        return {"executed": False, "plan": plan}
    if verdict.get("decision") == "blocked":
        raise HTTPException(status_code=409, detail=f"revision blocked: {verdict.get('reason')}")
    job = _enqueue_address(request, full_repo, number, {
        "trigger": "manual",
        "message": message,
        "fetch_comments": fetch,
        "review_feedback": {"comments": payload.get("comments") or []},
        "approved": bool(payload.get("approved")),
    })
    return {"executed": True, "plan": plan, **job}


@router.post("/api/prs/{owner}/{repo}/{number}/revision/approve")
def approve_revision(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    ensure_not_read_only()
    full_repo = f"{owner}/{repo}"
    record = _registry(request).get(full_repo, number)
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    pending = record.get("pending_revision") if isinstance(record.get("pending_revision"), dict) else None
    if not pending:
        raise HTTPException(status_code=409, detail="no revision is waiting for approval")
    feedback = pending.get("feedback") if isinstance(pending.get("feedback"), dict) else {"comments": pending.get("comments") or []}
    return _enqueue_address(request, full_repo, number, {"trigger": str(pending.get("trigger") or "review"), "review_feedback": feedback, "approved": True})


@router.post("/api/prs/{owner}/{repo}/{number}/revision/dismiss")
def dismiss_revision(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    ensure_not_read_only()
    registry = _registry(request)
    full_repo = f"{owner}/{repo}"
    if not registry.get(full_repo, number):
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    registry.update(full_repo, number, pending_revision=None)
    registry.append_decision(full_repo, number, {"decision": "dismissed", "by": get_actor_ids(request).get("user_id")})
    return {"ok": True}


@router.post("/api/prs/{owner}/{repo}/{number}/pause")
def pause_pr(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    ensure_not_read_only()
    record = _registry(request).set_watch(f"{owner}/{repo}", number, "paused", "paused by a person")
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    return _public_record(record)


@router.post("/api/prs/{owner}/{repo}/{number}/resume")
def resume_pr(owner: str, repo: str, number: int, request: Request) -> Dict[str, Any]:
    ensure_not_read_only()
    record = _registry(request).set_watch(f"{owner}/{repo}", number, "watching", None)
    if not record:
        raise HTTPException(status_code=404, detail="pull request is not tracked")
    return _public_record(record)
