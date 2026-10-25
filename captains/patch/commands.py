"""The patch captain's commands, for the open dispatcher (compass_core.dispatch): the feeds
and the inventory. The findings and the fix pull requests join here as the captain grows."""

from __future__ import annotations

from typing import Any, Dict

from captains.patch import feed as _feed
from captains.patch import inventory as _inventory
from captains.patch import pr as _pr  # noqa: F401 - registers the PR watcher's status hook
from compass_core import dispatch
from compass_core.workspace import captain_config, resolve


def _config(payload: Dict[str, Any]) -> tuple:
    workspace = resolve(payload)
    config = captain_config(payload, workspace)
    return config, str(workspace.workspace_id)


def feed_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the feeds once, now: what `compass patch feed_run` does the day a disclosure lands."""
    config, workspace_id = _config(payload)
    sources = payload.get("sources")
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    return _feed.run(config, workspace_id=workspace_id, sources=sources or None)


def feed_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    config, workspace_id = _config(payload)
    return _feed.status(config, workspace_id=workspace_id)


def feed_inject(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Test-only: put an advisory in the store (an OSV record or the store's own shape)."""
    config, workspace_id = _config(payload)
    advisory = payload.get("advisory")
    if not isinstance(advisory, dict):
        raise ValueError("advisory is required: an OSV record or an advisory dict, as advisory=<json> or in the payload file")
    return _feed.inject(config, workspace_id=workspace_id, advisory=advisory)


def advisories(payload: Dict[str, Any]) -> Dict[str, Any]:
    config, workspace_id = _config(payload)
    rows = _feed.advisories(config, workspace_id=workspace_id, limit=int(payload.get("limit") or 200))
    return {"advisories": rows, "count": len(rows)}


def inventory(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The software on the map as purls, per node, with exposure: what the feeds are asked about."""
    config, workspace_id = _config(payload)
    inv = _inventory.load(config, workspace_id=workspace_id)
    limit = int(payload.get("limit") or 500)
    return {"summary": inv.summary(), "items": [i.to_dict() for i in inv.items[:limit]], "truncated": len(inv.items) > limit}


def tick(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The match now: advisories × inventory → findings, the policy applied, the rows brought up to date."""
    from captains.patch import scheduler

    config, workspace_id = _config(payload)
    return scheduler.tick(config, workspace_id=workspace_id)


def findings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The findings: `state=open|approved|pr_open|merged|deployed|ignored|closed`, `exposed=true`, `exploited=true`, `repo=`."""
    from captains.patch import scheduler
    from captains.patch.findings import OPEN_STATES, summary

    config, workspace_id = _config(payload)
    _, store = scheduler.stores(config, workspace_id)
    rows = store.list()
    state = payload.get("state")
    chosen = [r for r in rows if (r.get("state") == state if state else r.get("state") in OPEN_STATES)
              and (not payload.get("exposed") or r.get("exposed")) and (not payload.get("exploited") or r.get("kev"))
              and (not payload.get("repo") or r.get("repo") == payload.get("repo"))]
    return {"summary": summary(rows), "findings": chosen[: int(payload.get("limit") or 200)], "count": len(chosen)}


def approve(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Approve findings (`keys=[...]` or `repo=`): the next tick opens their pull request."""
    from captains.patch import scheduler

    config, workspace_id = _config(payload)
    _, store = scheduler.stores(config, workspace_id)
    keys = payload.get("keys") or [r["key"] for r in store.list(states=["open"]) if r.get("repo") == payload.get("repo")]
    done = [store.approve(str(k), by=str(payload.get("by") or "cli")) for k in keys]
    return {"approved": sum(1 for r in done if r and r.get("state") == "approved")}


def ignore(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ignore a finding (`key=`) until a date (`until=YYYY-MM-DD`) with a reason (`reason=`)."""
    from captains.patch import scheduler

    config, workspace_id = _config(payload)
    _, store = scheduler.stores(config, workspace_id)
    key = str(payload.get("key") or "")
    if not key:
        raise ValueError("key is required: the finding's key from `compass patch findings`")
    row = store.ignore(key, by=str(payload.get("by") or "cli"), until=payload.get("until"), reason=payload.get("reason"))
    if row is None:
        raise LookupError(f"finding {key} not found")
    return {"key": key, "state": row.get("state"), "ignored": row.get("ignored")}


def pr(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Open or refresh the fix pull request of one repository now (`repo=`), or of every repository with pending findings."""
    from captains.patch import scheduler

    config, workspace_id = _config(payload)
    advisories_store, store = scheduler.stores(config, workspace_id)
    opener = _pr.PullRequestOpener(config, workspace_id=workspace_id, findings=store, advisories=advisories_store, model=scheduler._model(config))
    repo = payload.get("repo")
    if repo:
        group = _pr.pending(store.list(), config).get(str(repo)) or []
        if not group:
            raise LookupError(f"no pending finding in {repo}: nothing a pull request would change")
        result = opener.open(str(repo), group)
        return result or {"repo": repo, "skipped": "nothing to change on the base branch"}
    return opener.open_all()


COMMANDS = {"feed_run": feed_run, "feed_status": feed_status, "feed_inject": feed_inject, "advisories": advisories, "inventory": inventory,
            "tick": tick, "findings": findings, "approve": approve, "ignore": ignore, "pr": pr}

dispatch.register("patch", COMMANDS)
