from __future__ import annotations

from captains.prs import sync
from captains.prs.github_pr_client import COMPASS_MARKER, normalize_pull_state

NOW = 1_800_000_000_000
IDENTITY = {"emails": {"bot@example.com"}, "logins": {"compass-demo[bot]"}}
SETTINGS = {"mode": "autopilot", "risk_max": 2, "max_revisions": 3, "reply": True}


def _record(**over):
    base = {
        "repo": "acme/infra", "number": 7, "url": "https://github.com/acme/infra/pull/7", "type": "security_fix", "state": "open",
        "origin": {"captain": "engine", "finding_id": "f1"}, "intent": {"goal": "g"}, "watch": {"status": "watching"},
        "cursor": {}, "counters": {"revisions": 0, "refusals": 0, "human_commits": 0}, "revisions": [], "github": {}, "created_at": NOW - 3_600_000,
    }
    base.update(over)
    return base


def _snap(**over):
    base = {
        "state": "open", "updated_at": "2027-01-01T00:00:00Z", "updated_ms": NOW - 60_000, "title": "Fix", "draft": False,
        "head_sha": "aaa111", "branch": "beamreach/f1", "base_branch": "main", "mergeable_state": "clean", "review_decision": None,
        "auto_merge": False, "labels": [], "commits": [{"sha": "aaa111", "at_ms": NOW - 3_000_000, "author_email": "bot@example.com", "author_login": None, "committer_email": None, "committer_login": None}],
        "reviews": [], "threads": [], "issue_comments": [], "checks": {"state": None, "total": 0, "failed": 0, "pending": 0, "items": []},
    }
    base.update(over)
    return base


def _comment(cid, body, *, created_ms, assoc="MEMBER", path="a.tf", bot=False):
    return {"id": cid, "body": body, "path": path, "line": 3, "created_at": "2027-01-01T00:00:00Z", "created_ms": created_ms, "author": "alice", "bot": bot, "author_association": assoc}


def test_merged_and_closed_are_terminal():
    plan = sync.plan_sync(_record(), _snap(state="merged", merged_at="x"), settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.updates["state"] == "merged" and plan.updates["watch"]["status"] == "done"
    assert [e["event"] for e in plan.events] == ["pr.merged"] and plan.bookkeeping[0]["kind"] == "merged"
    plan = sync.plan_sync(_record(), _snap(state="closed"), settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.updates["state"] == "closed" and plan.events[0]["event"] == "pr.closed"
    again = sync.plan_sync(_record(state="merged"), _snap(state="merged"), settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert again.events == []


def test_new_review_comments_trigger_address_and_move_cursor():
    snap = _snap(threads=[
        {"resolved": False, "resolved_by": None, "comment": _comment(1, "rename it", created_ms=NOW - 10_000)},
        {"resolved": True, "resolved_by": "bob", "comment": _comment(2, "old", created_ms=NOW - 10_000)},
        {"resolved": False, "resolved_by": None, "comment": _comment(3, f"ack {COMPASS_MARKER}", created_ms=NOW - 5_000)},
        {"resolved": False, "resolved_by": None, "comment": _comment(4, "drive-by", created_ms=NOW - 4_000, assoc="NONE")},
    ], issue_comments=[_comment(9, "please also bump the provider", created_ms=NOW - 3_000, path=None)])
    plan = sync.plan_sync(_record(), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert len(plan.triggers) == 1 and plan.triggers[0]["trigger"] == "review"
    comments = plan.triggers[0]["review_feedback"]["comments"]
    assert [c["id"] for c in comments] == [1, 4, 9]
    assert [c["kind"] for c in comments] == ["change_in_scope", "ignore", "change_in_scope"]
    assert plan.updates["cursor"]["comment_ms"] == NOW - 3_000
    assert plan.events[0]["event"] == "pr.changes_requested"
    assert plan.updates["watch"]["next_poll_at"] == NOW + sync.POLL_FAST_MS
    # Second pass with the cursor moved: nothing new.
    again = sync.plan_sync(_record(cursor=plan.updates["cursor"]), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert again.triggers == []


def test_changes_requested_without_comments_still_triggers_once():
    snap = _snap(reviews=[{"state": "CHANGES_REQUESTED", "author": "bob", "bot": False, "at": "x"}], review_decision="CHANGES_REQUESTED")
    plan = sync.plan_sync(_record(), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.triggers[0]["review_feedback"]["requested_changes_by"] == ["bob"]
    assert plan.updates["github"]["requested_changes_by"] == ["bob"]
    again = sync.plan_sync(_record(cursor=plan.updates["cursor"]), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert again.triggers == []


def test_ci_failure_and_conflicts():
    snap = _snap(mergeable_state="dirty", checks={"state": "failure", "total": 2, "failed": 1, "pending": 0, "items": [{"name": "terraform validate", "status": "failure", "summary": "boom", "text": "err", "url": None}, {"name": "fmt", "status": "success"}]})
    plan = sync.plan_sync(_record(), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    kinds = [t["trigger"] for t in plan.triggers]
    assert kinds == ["conflict", "ci"]
    assert plan.triggers[1]["review_feedback"]["ci_failures"][0]["name"] == "terraform validate"
    assert "pr.ci_failed" in [e["event"] for e in plan.events]
    again = sync.plan_sync(_record(cursor=plan.updates["cursor"]), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert again.triggers == []


def test_human_commit_stops_pushing():
    snap = _snap(commits=[
        {"sha": "aaa111", "at_ms": NOW - 3_000_000, "author_email": "bot@example.com", "author_login": None, "committer_email": None, "committer_login": None},
        {"sha": "bbb222", "at_ms": NOW - 1_000, "author_email": "dev@acme.com", "author_login": "dev", "committer_email": "dev@acme.com", "committer_login": "dev"},
    ], head_sha="bbb222", threads=[{"resolved": False, "resolved_by": None, "comment": _comment(1, "x", created_ms=NOW - 500)}])
    plan = sync.plan_sync(_record(), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.updates["watch"]["status"] == "needs_human" and plan.updates["watch"]["reason"] == "human_commit"
    assert plan.updates["counters"]["human_commits"] == 1
    assert plan.triggers == []  # no pushes after a person pushed
    assert plan.events[0]["event"] == "pr.needs_human"
    bot_snap = _snap(commits=[{"sha": "ccc", "at_ms": NOW - 1_000, "author_email": "x@y", "author_login": "compass-demo[bot]", "committer_email": None, "committer_login": None}])
    assert sync.plan_sync(_record(), bot_snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY).updates["watch"]["status"] == "watching"


def test_ready_and_stale_and_backoff():
    snap = _snap(review_decision="APPROVED", checks={"state": "success", "total": 2, "failed": 0, "pending": 0, "items": []})
    plan = sync.plan_sync(_record(), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.updates["github"]["ready"] is True and plan.events[0]["event"] == "pr.ready"
    again = sync.plan_sync(_record(cursor=plan.updates["cursor"]), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert again.events == []
    idle = sync.plan_sync(_record(), _snap(updated_ms=NOW - 8 * sync.DAY_MS), settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert idle.updates["watch"]["next_poll_at"] == NOW + sync.POLL_IDLE_MS
    stale = sync.plan_sync(_record(), _snap(updated_ms=NOW - 31 * sync.DAY_MS), settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert stale.updates["watch"]["status"] == "done" and stale.updates["watch"]["reason"] == "stale"


def test_paused_record_only_tracks_state():
    snap = _snap(threads=[{"resolved": False, "resolved_by": None, "comment": _comment(1, "x", created_ms=NOW - 500)}])
    plan = sync.plan_sync(_record(watch={"status": "paused"}), snap, settings=SETTINGS, now_ms=NOW, identity=IDENTITY)
    assert plan.triggers == [] and plan.updates["github"]["head_sha"] == "aaa111"


def test_normalize_pull_state():
    node = {
        "state": "OPEN", "merged": False, "updatedAt": "2027-01-01T00:00:00Z", "title": "t", "headRefOid": "abc", "headRefName": "b", "baseRefName": "main",
        "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "autoMergeRequest": None, "labels": {"nodes": [{"name": "compass:hold"}]},
        "commits": {"nodes": [{"commit": {"oid": "abc", "committedDate": "2027-01-01T00:00:00Z", "author": {"name": "B", "email": "bot@example.com", "user": None}, "committer": {"name": "B", "email": "bot@example.com", "user": None}}}]},
        "latestReviews": {"nodes": [{"state": "APPROVED", "submittedAt": "x", "author": {"login": "bob", "__typename": "User"}}]},
        "reviewThreads": {"nodes": [{"isResolved": False, "resolvedBy": None, "comments": {"nodes": [{"databaseId": 5, "body": "hi", "path": "a.tf", "line": 2, "createdAt": "2027-01-01T00:00:00Z", "authorAssociation": "MEMBER", "author": {"login": "bob", "__typename": "User"}}]}}]},
        "comments": {"nodes": [{"databaseId": 6, "body": "bot says", "createdAt": "2027-01-01T00:00:00Z", "authorAssociation": "NONE", "author": {"login": "x[bot]", "__typename": "Bot"}}]},
        "headCommit": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "validate", "conclusion": "FAILURE", "status": "COMPLETED", "summary": "s", "text": "t", "detailsUrl": "u"},
            {"__typename": "StatusContext", "context": "ci/lint", "state": "PENDING", "description": "d", "targetUrl": "u2"},
        ]}}}}]},
    }
    snap = normalize_pull_state(node)
    assert snap["state"] == "open" and snap["mergeable_state"] == "clean" and snap["labels"] == ["compass:hold"]
    assert snap["threads"][0]["comment"]["id"] == 5 and snap["threads"][0]["comment"]["author_association"] == "MEMBER"
    assert snap["issue_comments"][0]["bot"] is True
    assert snap["checks"] == {"state": "failure", "total": 2, "failed": 1, "pending": 1, "items": snap["checks"]["items"]}
    assert snap["commits"][0]["author_email"] == "bot@example.com" and snap["commits"][0]["at_ms"] > 0
