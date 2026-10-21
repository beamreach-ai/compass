"""The demo back to its golden world: ``compass demo reset`` (docs/demo.md), and what
``compass demo status`` prints.

The reset is idempotent and runs in one order, each step counted: the decoys' part (the
captain that owns them does it: incidents closed, keys rolled back, decoys re-armed, the
replay service at steady state), the visitors' pull requests closed on GitHub with their
branches deleted and their decoys and grants retired without a pull request, the seeded
pull request recreated with the reviewer's comment, old jobs pruned, the trips and tours
cleared, the world seeded again, a rescan started, and a ``DEMO#RESET#<ts>`` row with the
counts. Scheduled for Sunday 03:00 UTC by the deployment, runnable by hand any time.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from compass_core import demo, grants
from compass_core.github import client as github_client
from compass_core.github import resolve_github_token
from compass_core.github_prs import commit_files, ensure_branch, open_or_update_pr
from compass_core.jobs import store_for
from compass_core.keys import demo_reset_prefix, demo_reset_sort_key, honeytoken_sort_key, workspace_pk
from compass_core.registry import PRRegistry
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("compass.demo.reset")

SEEDED_BRANCH = "beamreach/demo/payments-variables"
SEEDED_TITLE = "payments: describe the service's variables"
SEEDED_BODY = ("The payments service's variables, with descriptions. Opened by the demo's reset: a reviewer's request is "
               "waiting on it under Pull requests → Needs you, so you can watch Compass answer one.\n\n"
               "This repository is never applied; in your install you would review and merge it. Closed on Sunday.")
SEEDED_FILE = '''variable "queue_visibility_timeout" {
  description = "How long a payment stays invisible after a worker takes it."
  type        = number
  default     = 30
}

variable "retry_limit" {
  description = "Retries before a payment goes to the dead-letter queue."
  type        = number
  default     = 3
}
'''
SEEDED_COMMENT = ("Could you bump `queue_visibility_timeout` to 60 and say in its description why? Workers took longer than "
                  "30 s under last week's load and payments were retried twice.")
VISITOR_LABEL = "demo"


def reset(config: Dict[str, Any], *, workspace_id: str, decoys: Callable[[], Dict[str, Any]], rescan: Callable[[], Optional[str]],
          notify: Optional[Callable[[str], None]] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Everything back; ``decoys`` and ``rescan`` are the captains' parts, handed in by the command."""
    demo.require_demo_mode("demo reset")
    started = time.monotonic()
    now_ms = now_ms or demo.epoch_ms()
    settings = demo.settings(config)
    table = dynamo_table(tables(config).items)
    pk = workspace_pk(workspace_id)
    out: Dict[str, Any] = {"workspace_id": workspace_id, "started_at": now_ms}

    out["decoys"] = decoys()
    out["pull_requests"] = _close_visitor_prs(config, settings, table, pk, workspace_id=workspace_id)
    out["seeded_pr"] = _seed_pr(config, settings, workspace_id=workspace_id)
    out["jobs_pruned"] = _prune_jobs(config, workspace_id=workspace_id, days=int(settings["reset"]["prune_jobs_days"]), now_ms=now_ms)
    out["rows_cleared"] = _clear_demo_rows(table, pk)
    out["world"] = _reseed(workspace_id)
    out["rescan_job_id"] = rescan()
    out["duration_ms"] = int((time.monotonic() - started) * 1000)  # DynamoDB takes no floats
    table.put_item(Item={"pk": pk, "sk": demo_reset_sort_key(now_ms), "reset_at": now_ms, **{k: v for k, v in out.items() if k != "workspace_id"}})
    line = (f"Demo reset: {out['decoys'].get('incidents_closed', 0)} incidents closed, {out['pull_requests']['closed']} visitor pull requests closed, "
            f"the seeded pull request {'recreated' if out['seeded_pr'].get('url') else 'not recreated'}, {out['jobs_pruned']} jobs pruned, "
            f"the world {'unchanged' if out['world'].get('unchanged') else 'reseeded'}, in {out['duration_ms'] / 1000:.1f} s")
    out["summary"] = line
    logger.info("%s", line)
    if notify is not None:
        try:
            notify(line)
        except Exception as exc:  # noqa: BLE001 - the reset is done; the line was a courtesy
            logger.warning("demo reset: could not post the summary: %s", exc)
    return out


# ── the visitors' pull requests ─────────────────────────────────────────────

def _close_visitor_prs(config: Dict[str, Any], settings: Dict[str, Any], table: Any, pk: str, *, workspace_id: str) -> Dict[str, Any]:
    """Every open pull request Compass opened on the visitor repository: closed on GitHub, its
    branch deleted, its decoy and grants retired with no pull request of their own."""
    registry = PRRegistry.from_config(config, workspace_id=workspace_id)
    repo_name = str(settings.get("visitor_repo") or "")
    records = [r for r in registry.list(limit=500) if not repo_name or r.get("repo") == repo_name]
    out: Dict[str, Any] = {"closed": 0, "branches_deleted": 0, "decoys_retired": 0, "grants_retired": 0, "failed": []}
    if not records:
        return out
    gh = github_client(resolve_github_token(config, capability="write"))
    for record in records:
        repo, number = str(record.get("repo")), int(record.get("number") or 0)
        was_open = (record.get("state") or "open") == "open"
        try:
            gh_repo = gh.get_repo(repo)
            if was_open:
                pull = gh_repo.get_pull(number)
                if pull.state != "closed":
                    pull.edit(state="closed")
                out["closed"] += 1
                registry.update(repo, number, state="closed", watch={**(record.get("watch") or {}), "status": "done", "reason": "demo reset", "next_poll_at": None})
            # the branch goes whether the pull request was open, merged or closed: a visitor's week leaves none behind
            branch = str(record.get("branch") or "")
            if branch and branch != str(record.get("base_branch") or ""):
                try:
                    gh_repo.get_git_ref(f"heads/{branch}").delete()
                    out["branches_deleted"] += 1
                except Exception:  # noqa: BLE001 - already gone
                    pass
        except Exception as exc:  # noqa: BLE001
            out["failed"].append({"repo": repo, "number": number, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if not was_open:
            continue
        origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
        if origin.get("honeytoken_id"):
            out["decoys_retired"] += _retire_decoy(table, pk, str(origin["honeytoken_id"]))
        for grant in grants.list_grants(table, pk):
            if grant.get("grant_pr") and grant.get("grant_pr") == record.get("url") and str(grant.get("grant_status")) not in ("revoked", "declined", "disarmed"):
                try:
                    grants.remove(table, pk, str(grant["action_id"]), config=config, actor="demo-reset", enqueue=lambda **kw: None)
                    out["grants_retired"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["failed"].append({"grant": grant.get("action_id"), "error": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def _retire_decoy(table: Any, pk: str, honeytoken_id: str) -> int:
    """A visitor's planted decoy: never applied, so retired in place, no decommission pull request."""
    key = {"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}
    item = table.get_item(Key=key).get("Item")
    if not item or item.get("status") == "decommissioned":
        return 0
    table.update_item(Key=key, UpdateExpression="SET #st = :s, updated_at = :now", ExpressionAttributeNames={"#st": "status"},
                      ExpressionAttributeValues={":s": "decommissioned", ":now": demo.epoch_ms()})
    return 1


# ── the seeded pull request ─────────────────────────────────────────────────

def _seed_pr(config: Dict[str, Any], settings: Dict[str, Any], *, workspace_id: str) -> Dict[str, Any]:
    """One fixed file change on the visitor repository and one reviewer's comment on it (the
    bot account's token when the block names one, else Compass's own), registered so the PR
    watcher syncs it and lists it under Needs you."""
    repo_name = str(settings.get("visitor_repo") or "")
    seeded = settings.get("seeded_pr") or {}
    if not repo_name or seeded.get("enabled") is False:
        return {"skipped": "no visitor repository" if not repo_name else "disabled"}
    path = str(seeded.get("path") or "services/payments/variables.tf")
    base = str(seeded.get("base_branch") or "main")
    token = resolve_github_token(config, capability="write")
    gh_repo = github_client(token).get_repo(repo_name)
    ensure_branch(gh_repo, SEEDED_BRANCH, base)
    commit_files(gh_repo, SEEDED_BRANCH, {path: SEEDED_FILE}, add_message="payments: describe the service's variables",
                 update_message="payments: describe the service's variables")
    published = open_or_update_pr(gh_repo, branch=SEEDED_BRANCH, base_branch=base, title=SEEDED_TITLE, body=SEEDED_BODY)
    pull = gh_repo.get_pull(published.number)
    try:
        pull.add_to_labels(VISITOR_LABEL)
    except Exception:  # noqa: BLE001 - a repository without the label still gets the pull request
        pass
    reviewer_token = str(seeded.get("reviewer_token") or "") or token
    reviewer = github_client(reviewer_token).get_repo(repo_name).get_pull(published.number)
    reviewer.create_review(body=str(seeded.get("comment") or SEEDED_COMMENT), event="REQUEST_CHANGES")
    registry = PRRegistry.from_config(config, workspace_id=workspace_id)
    registry.record(url=published.url, pr_type="demo_seeded", origin={"command": "seed_pr", "seeded": True, "workspace_id": workspace_id},
                    intent={"goal": "describe the payments service's variables", "scope": {"files": [path]}, "action_family": "iac_change"},
                    branch=SEEDED_BRANCH, base_branch=base, title=SEEDED_TITLE)
    return {"url": published.url, "number": published.number, "reviewer": seeded.get("reviewer") or "compass"}


# ── jobs, rows, the world ───────────────────────────────────────────────────

def _prune_jobs(config: Dict[str, Any], *, workspace_id: str, days: int, now_ms: int) -> int:
    store = store_for(config)
    if store is None or getattr(store, "_table", None) is None:
        return 0
    cutoff = now_ms - int(days) * 86_400_000
    pruned = 0
    for job in store.list_jobs(limit=10_000, workspace_id=workspace_id):
        if int(job.get("created_at") or 0) < cutoff and job.get("status") in ("completed", "failed", "dead", "cancelled"):
            store._table.delete_item(Key=store._key(str(job["job_id"]), workspace_id))
            pruned += 1
    return pruned


def _clear_demo_rows(table: Any, pk: str) -> int:
    """The trips, the visitors' hourly counts and the tours; the resets stay."""
    cleared = 0
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("DEMO#"), "ProjectionExpression": "pk, sk"}
    while True:
        page = table.query(**kwargs)
        for item in page.get("Items", []):
            if not str(item["sk"]).startswith(demo_reset_prefix()):
                table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                cleared += 1
        if not page.get("LastEvaluatedKey"):
            return cleared
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _reseed(workspace_id: str) -> Dict[str, Any]:
    result = demo.seed({"workspace_id": workspace_id})
    scan = result.get("scan") or {}
    return {"scope": result.get("scope"), "nodes": result.get("nodes"), "edges": result.get("edges"),
            "unchanged": not (scan.get("added") or scan.get("changed") or scan.get("removed")), "scan": scan}


# ── status ──────────────────────────────────────────────────────────────────

_CRON = re.compile(r"^\s*cron\(\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*\)\s*$")
_DAYS = {"SUN": 6, "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5}


def next_run(schedule: str, *, after: Optional[datetime] = None) -> Optional[datetime]:
    """The next time of an EventBridge Scheduler ``cron(m h ? * DOW *)`` with one weekday, UTC;
    None for a shape this does not read (the schedule still runs, we just do not print it)."""
    match = _CRON.match(schedule or "")
    if not match:
        return None
    minute, hour, day_of_month, month, day_of_week, year = match.groups()
    if day_of_month != "?" or month != "*" or year != "*" or not minute.isdigit() or not hour.isdigit():
        return None
    weekday = _DAYS.get(day_of_week.upper()) if not day_of_week.isdigit() else (int(day_of_week) + 5) % 7
    if weekday is None:
        return None
    after = after or datetime.now(timezone.utc)
    candidate = after.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    while candidate.weekday() != weekday or candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def status(config: Dict[str, Any], *, workspace_id: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The last reset, the next one, the trips in force, the visitors' open pull requests, the
    tours that reached the end this week."""
    demo.require_demo_mode("demo status")
    now_ms = now_ms or demo.epoch_ms()
    settings = demo.settings(config)
    table = dynamo_table(tables(config).items)
    pk = workspace_pk(workspace_id)
    resets = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(demo_reset_prefix()), ScanIndexForward=False, Limit=1).get("Items", [])
    last = demo._native(resets[0]) if resets else None
    upcoming = next_run(str(settings["reset"]["schedule"]))
    repo_name = str(settings.get("visitor_repo") or "")
    records = PRRegistry.from_config(config, workspace_id=workspace_id).list(limit=500)
    visitor_prs = [r for r in records if (r.get("state") or "open") == "open" and (not repo_name or r.get("repo") == repo_name)
                   and not ((r.get("origin") or {}).get("seeded"))]
    return {
        "workspace_id": workspace_id,
        "last_reset": {k: v for k, v in (last or {}).items() if k not in ("pk", "sk")} or None,
        "next_reset": upcoming.isoformat() if upcoming else None,
        "schedule": settings["reset"]["schedule"],
        "cooldowns_in_force": [r for r in demo.cooldowns(table, pk, now_ms=now_ms) if r["in_force"]],
        "visitor_prs_open": [{"repo": r.get("repo"), "number": r.get("number"), "url": r.get("url"), "type": r.get("type")} for r in visitor_prs],
        "tour_completions_this_week": demo.tour_completions(table, pk, since_ms=now_ms - 7 * 86_400_000),
        "world": settings["world"],
    }


__all__: List[str] = ["reset", "status", "next_run"]
