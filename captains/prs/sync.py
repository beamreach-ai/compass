"""The transition table of ``prs.sync`` (docs/pr_watcher_spec.md §4.1), as a pure function.

``plan_sync`` looks at one PR record and one normalised GitHub snapshot and
returns what to store, which ``prs.address`` jobs to enqueue, which events to
emit and which origin bookkeeping to do. No I/O, so every branch has a test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from captains.prs.github_pr_client import COMPASS_MARKER, TRUSTED_ASSOCIATIONS

# The email on commits Compass makes when the config names none; a deployment that commits
# under its own identity sets COMPASS_AUTHOR_EMAIL (or github.author_email in the config).
DEFAULT_AUTHOR_EMAIL = os.getenv("COMPASS_AUTHOR_EMAIL", "compass@localhost")

DAY_MS = 24 * 3600 * 1000
STALE_AFTER_MS = 30 * DAY_MS
POLL_FAST_MS = 5 * 60 * 1000
POLL_SLOW_MS = 60 * 60 * 1000
POLL_IDLE_MS = 6 * 60 * 60 * 1000
HOLD_LABEL = "compass:hold"
NEEDS_HUMAN_LABEL = "compass:needs-human"


@dataclass
class SyncPlan:
    updates: Dict[str, Any] = field(default_factory=dict)
    triggers: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    bookkeeping: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def compass_identity(config: Dict[str, Any]) -> Dict[str, Any]:
    github = config.get("github") if isinstance(config.get("github"), dict) else {}
    emails = {str(github.get("author_email") or DEFAULT_AUTHOR_EMAIL).lower()}
    logins = set()
    slug = github.get("app_slug")
    if isinstance(slug, str) and slug.strip():
        logins.add(f"{slug.strip()}[bot]".lower())
    for key in ("bot_login", "login"):
        value = github.get(key)
        if isinstance(value, str) and value.strip():
            logins.add(value.strip().lower())
    return {"emails": emails, "logins": logins}


def _is_compass_commit(commit: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    emails = identity.get("emails") or set()
    logins = identity.get("logins") or set()
    for key in ("author_email", "committer_email"):
        value = commit.get(key)
        if isinstance(value, str) and value.lower() in emails:
            return True
    for key in ("author_login", "committer_login"):
        value = commit.get(key)
        if isinstance(value, str) and (value.lower() in logins or value.lower().endswith("[bot]")):
            return True
    return False


def _known_shas(record: Dict[str, Any]) -> set:
    shas = set()
    for rev in record.get("revisions") or []:
        sha = (rev or {}).get("commit_sha")
        if isinstance(sha, str) and sha:
            shas.add(sha)
    github = record.get("github") if isinstance(record.get("github"), dict) else {}
    if github.get("head_sha"):
        shas.add(str(github["head_sha"]))
    return shas


def _feedback_comment(comment: Dict[str, Any], source: str) -> Dict[str, Any]:
    trusted = comment.get("author_association") in TRUSTED_ASSOCIATIONS
    return {
        "id": comment.get("id"),
        "source": source,
        "path": comment.get("path"),
        "line": comment.get("line"),
        "body": comment.get("body"),
        "author": comment.get("author"),
        "author_association": comment.get("author_association"),
        "created_at": comment.get("created_at"),
        "kind": "change_in_scope" if trusted else "ignore",
        "reason": None if trusted else "author is not a repository collaborator",
    }


def _poll_interval(last_activity_ms: int, now_ms: int) -> int:
    age = max(0, now_ms - last_activity_ms)
    if age < DAY_MS:
        return POLL_FAST_MS
    if age < 7 * DAY_MS:
        return POLL_SLOW_MS
    return POLL_IDLE_MS


def plan_sync(record: Dict[str, Any], snap: Dict[str, Any], *, settings: Dict[str, Any], now_ms: int, identity: Dict[str, Any]) -> SyncPlan:
    plan = SyncPlan()
    repo, number = record.get("repo"), record.get("number")
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    watch = dict(record.get("watch") or {"status": "watching"})
    cursor = dict(record.get("cursor") or {})
    counters = dict(record.get("counters") or {})
    checks = snap.get("checks") if isinstance(snap.get("checks"), dict) else {}
    last_activity_ms = int(snap.get("updated_ms") or record.get("updated_at") or now_ms)

    plan.updates["github"] = {
        "head_sha": snap.get("head_sha"),
        "updated_at": snap.get("updated_at"),
        "mergeable_state": snap.get("mergeable_state"),
        "review_decision": snap.get("review_decision"),
        # the counts, and the names with their status: the patch captain tells the image build apart by name
        "checks": {**{k: checks.get(k) for k in ("state", "total", "failed", "pending")},
                   "items": [{"name": i.get("name"), "status": i.get("status")} for i in (checks.get("items") or []) if isinstance(i, dict) and i.get("name")][:50]},
        "labels": list(snap.get("labels") or []),
        "auto_merge": bool(snap.get("auto_merge")),
        "draft": bool(snap.get("draft")),
        "last_activity_at": snap.get("updated_at"),
        "requested_changes_by": [r.get("author") for r in (snap.get("reviews") or []) if r.get("state") == "CHANGES_REQUESTED" and r.get("author")],
        "synced_at": now_ms,
    }
    if snap.get("title"):
        plan.updates["title"] = snap["title"]
    if snap.get("branch"):
        plan.updates["branch"] = snap["branch"]
    if snap.get("base_branch"):
        plan.updates["base_branch"] = snap["base_branch"]

    # Terminal states first.
    if snap.get("state") == "merged":
        if record.get("state") != "merged":
            plan.events.append({"event": "pr.merged", "payload": {"repo": repo, "number": number, "merged_at": snap.get("merged_at")}})
            plan.bookkeeping.append({"kind": "merged", "origin": origin})
        plan.updates["state"] = "merged"
        plan.updates["watch"] = {**watch, "status": "done", "reason": "merged", "next_poll_at": None}
        return plan
    if snap.get("state") == "closed":
        if record.get("state") != "closed":
            plan.events.append({"event": "pr.closed", "payload": {"repo": repo, "number": number, "closed_at": snap.get("closed_at")}})
            plan.bookkeeping.append({"kind": "closed", "origin": origin})
        plan.updates["state"] = "closed"
        plan.updates["watch"] = {**watch, "status": "done", "reason": "closed", "next_poll_at": None}
        return plan
    if record.get("state") in ("merged", "closed"):
        plan.updates["state"] = "open"  # reopened
        watch = {"status": "watching", "reason": None, "backoff_level": 0}

    status = str(watch.get("status") or "watching")
    interval = _poll_interval(last_activity_ms, now_ms)

    # A person pushed to the branch: Compass stops pushing for good (until resume).
    human = [
        c for c in (snap.get("commits") or [])
        if c.get("sha") and c["sha"] not in _known_shas(record)
        and not _is_compass_commit(c, identity)
        and int(c.get("at_ms") or 0) > int(record.get("created_at") or 0) + 60_000
        and c["sha"] != cursor.get("human_commit_sha")
    ]
    if human and status in ("watching", "paused"):
        counters["human_commits"] = int(counters.get("human_commits") or 0) + len(human)
        cursor["human_commit_sha"] = human[-1]["sha"]
        watch = {**watch, "status": "needs_human", "reason": "human_commit"}
        plan.events.append({"event": "pr.needs_human", "payload": {"repo": repo, "number": number, "reason": "human_commit", "commit": human[-1]["sha"], "author": human[-1].get("author_login") or human[-1].get("author_email")}})
        plan.notes.append(f"human commit {str(human[-1]['sha'])[:7]}")
        status = "needs_human"

    if HOLD_LABEL in (snap.get("labels") or []):
        plan.notes.append("hold label present")

    if status == "watching":
        # Conflicts with the base branch.
        if snap.get("mergeable_state") == "dirty" and snap.get("head_sha") != cursor.get("conflict_sha"):
            cursor["conflict_sha"] = snap.get("head_sha")
            plan.triggers.append({"trigger": "conflict", "review_feedback": {"trigger": "conflict", "comments": [], "ci_failures": [], "conflicts": [], "head_sha": snap.get("head_sha")}})

        # Failing checks on a head Compass pushed.
        if int(checks.get("failed") or 0) > 0 and snap.get("head_sha") != cursor.get("ci_sha") and not human:
            cursor["ci_sha"] = snap.get("head_sha")
            failures = [{"name": i.get("name"), "summary": i.get("summary"), "text": (i.get("text") or "")[:4000], "url": i.get("url")} for i in (checks.get("items") or []) if i.get("status") == "failure"]
            plan.triggers.append({"trigger": "ci", "review_feedback": {"trigger": "ci", "comments": [], "ci_failures": failures, "conflicts": [], "head_sha": snap.get("head_sha")}})
            plan.events.append({"event": "pr.ci_failed", "payload": {"repo": repo, "number": number, "checks": [f["name"] for f in failures]}})

        # New feedback past the cursor.
        since = int(cursor.get("comment_ms") or 0)
        comments: List[Dict[str, Any]] = []
        newest = since
        for thread in snap.get("threads") or []:
            c = thread.get("comment") if isinstance(thread, dict) else None
            if not c or thread.get("resolved") or c.get("bot") or COMPASS_MARKER in str(c.get("body") or ""):
                continue
            if int(c.get("created_ms") or 0) <= since:
                continue
            comments.append(_feedback_comment(c, "review_comment"))
            newest = max(newest, int(c.get("created_ms") or 0))
        for c in snap.get("issue_comments") or []:
            if c.get("bot") or COMPASS_MARKER in str(c.get("body") or "") or int(c.get("created_ms") or 0) <= since:
                continue
            comments.append(_feedback_comment(c, "issue_comment"))
            newest = max(newest, int(c.get("created_ms") or 0))
        requested_by = plan.updates["github"]["requested_changes_by"]
        if comments or (requested_by and cursor.get("changes_requested_sha") != snap.get("head_sha")):
            cursor["comment_ms"] = newest or now_ms
            if requested_by:
                cursor["changes_requested_sha"] = snap.get("head_sha")
            comments.sort(key=lambda c: c.get("created_at") or "")
            plan.triggers.append({"trigger": "review", "review_feedback": {"trigger": "review", "comments": comments, "ci_failures": [], "conflicts": [], "requested_changes_by": requested_by, "head_sha": snap.get("head_sha")}})
            if any(c["kind"] != "ignore" for c in comments) or requested_by:
                plan.events.append({"event": "pr.changes_requested", "payload": {"repo": repo, "number": number, "comments": len(comments), "by": requested_by}})

        # Ready to merge.
        ready = (
            snap.get("review_decision") in ("APPROVED", None) and snap.get("review_decision") is not None
            and int(checks.get("failed") or 0) == 0 and int(checks.get("pending") or 0) == 0
            and snap.get("mergeable_state") in ("clean", "has_hooks", "unstable")
        ) or (snap.get("review_decision") is None and snap.get("mergeable_state") == "clean" and int(checks.get("failed") or 0) == 0 and int(checks.get("pending") or 0) == 0 and int(checks.get("total") or 0) > 0)
        if ready and cursor.get("ready_sha") != snap.get("head_sha"):
            cursor["ready_sha"] = snap.get("head_sha")
            plan.events.append({"event": "pr.ready", "payload": {"repo": repo, "number": number, "head_sha": snap.get("head_sha")}})
        plan.updates["github"]["ready"] = bool(ready)

        if now_ms - last_activity_ms > STALE_AFTER_MS:
            watch = {**watch, "status": "done", "reason": "stale"}
            plan.notes.append("stale")

    if plan.triggers:
        interval = POLL_FAST_MS
    watch["next_poll_at"] = now_ms + interval
    plan.updates["watch"] = watch
    plan.updates["cursor"] = cursor
    plan.updates["counters"] = counters
    return plan
