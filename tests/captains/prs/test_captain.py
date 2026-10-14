from __future__ import annotations

import pytest

from captains.prs import captain as captain_module
from captains.prs.captain import PRCaptain
from compass_core import queue as core_queue
from compass_core import registry as pr_registry


class FakeClient:
    def __init__(self):
        self.replies = []
        self.comments = []
        self.reviewers = []
        self.feedback = {"comments": [], "requested_changes_by": [], "head_sha": "abc"}

    def fetch_open_feedback(self, repo, number, since_ms=None):
        return self.feedback

    def reply_to_review_comment(self, repo, number, comment_id, body):
        self.replies.append((comment_id, body))

    def create_issue_comment(self, repo, number, body):
        self.comments.append(body)

    def request_reviewers(self, repo, number, logins):
        self.reviewers.extend(logins)


@pytest.fixture
def setup(monkeypatch):
    pr_registry.reset_memory_for_tests()
    monkeypatch.delenv("READ_ONLY", raising=False)
    client = FakeClient()
    monkeypatch.setattr(PRCaptain, "_client", lambda self, repo: client)
    monkeypatch.setattr(PRCaptain, "_bedrock", lambda self: None)
    monkeypatch.setattr(captain_module, "load_origin_context", lambda record, **_k: {"summary": "ctx", "changed_since_pr": [], "notes": []})
    config = {"workspace_id": "ws-1", "target": "demo", "prs": {"watcher": {"mode": "autopilot", "risk_max": 2}}}
    cap = PRCaptain(config=config)
    cap.registry.record(
        url="https://github.com/acme/infra/pull/7", pr_type="security_fix", origin={"captain": "engine", "finding_id": "f1"},
        intent={"goal": "Block public access", "risk": 2, "scope": {"files": ["modules/storage.tf"]}, "invariants": [{"kind": "files_differ_from_base", "files": ["modules/storage.tf"]}]},
        branch="beamreach/f1", base_branch="main",
    )
    yield cap, client, config
    pr_registry.reset_memory_for_tests()


def _comment(cid=101, path="modules/storage.tf", body="rename it", kind="change_in_scope"):
    return {"id": cid, "source": "review_comment", "path": path, "line": 3, "body": body, "author": "alice", "author_association": "MEMBER", "kind": kind}


def test_pushed_revision_replies_and_records(monkeypatch, setup):
    cap, client, _ = setup
    client.feedback = {"comments": [_comment()], "requested_changes_by": ["alice"], "head_sha": "abc"}
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: {"outcome": "pushed", "reason": None, "commit_sha": "deadbeef1234", "pr_url": k["record"]["url"], "changed_files": ["modules/storage.tf"], "guard": {"ok": True, "checks": [{"name": "scope", "ok": True}]}, "blockers": []})
    result = cap.address({"repo": "acme/infra", "number": 7, "fetch_comments": True, "trigger": "review"})
    assert result["status"] == "pushed" and result["commit_sha"] == "deadbeef1234"
    record = cap.registry.get("acme/infra", 7)
    assert record["counters"]["revisions"] == 1
    assert record["revisions"][0]["addressed"] == [101] and record["revisions"][0]["context"]["summary"] == "ctx"
    assert record["decisions"][-1]["decision"] == "run"
    assert client.replies[0][0] == 101 and "Addressed in deadbee" in client.replies[0][1]
    assert client.comments and "Pushed deadbee" in client.comments[0]
    assert client.reviewers == ["alice"]


def test_refusal_flags_human_and_explains(monkeypatch, setup):
    cap, client, _ = setup
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: {"outcome": "refused", "reason": "invariant_broken", "guard": {"ok": False, "reason": "invariant_broken", "checks": [{"name": "files_differ_from_base", "ok": False, "detail": "reverted: modules/storage.tf"}]}, "changed_files": [], "blockers": []})
    result = cap.address({"repo": "acme/infra", "number": 7, "message": "remove the policy", "trigger": "manual"})
    assert result["status"] == "refused"
    record = cap.registry.get("acme/infra", 7)
    assert record["watch"]["status"] == "needs_human" and record["watch"]["reason"] == "invariant_broken"
    assert record["counters"]["refusals"] == 1
    assert "did not push" in client.comments[0] and "Block public access" in client.comments[0]
    assert cap.address({"repo": "acme/infra", "number": 7, "message": "again", "trigger": "manual"})["status"] == "blocked"


def test_approve_mode_stores_a_pending_plan(monkeypatch, setup):
    cap, client, config = setup
    config["prs"]["watcher"]["mode"] = "approve"
    called = []
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: called.append(1))
    result = cap.address({"repo": "acme/infra", "number": 7, "review_feedback": {"comments": [_comment()]}, "trigger": "review"})
    assert result["status"] == "pending_approval" and not called
    pending = cap.registry.get("acme/infra", 7)["pending_revision"]
    assert pending["comments"][0]["id"] == 101 and pending["risk"]["risk"] == 2
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: {"outcome": "pushed", "commit_sha": "c0ffee00", "changed_files": [], "guard": {"ok": True, "checks": []}, "blockers": []})
    approved = cap.address({"repo": "acme/infra", "number": 7, "review_feedback": pending["feedback"], "approved": True, "trigger": "review"})
    assert approved["status"] == "pushed"
    assert cap.registry.get("acme/infra", 7)["pending_revision"] is None


def test_untrusted_authors_only_get_a_note(monkeypatch, setup):
    cap, client, _ = setup
    client.feedback = {"comments": [{**_comment(cid=5), "kind": "ignore", "author_association": "NONE"}], "requested_changes_by": [], "head_sha": "abc"}
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: (_ for _ in ()).throw(AssertionError("must not run")))
    result = cap.address({"repo": "acme/infra", "number": 7, "fetch_comments": True, "trigger": "review"})
    assert result["status"] == "nothing_to_do" and result["ignored"] == 1


def test_sync_applies_transitions_and_enqueues_address(monkeypatch, setup):
    cap, client, _ = setup
    enqueued = []

    class FakeQueue:
        def enqueue(self, **kwargs):
            enqueued.append(kwargs)
            return {"job_id": "j", "status": "queued"}

    monkeypatch.setattr(core_queue, "_factory", lambda: FakeQueue())
    emitted = []
    monkeypatch.setattr(captain_module.notify, "emit", lambda config, ws, event, payload, **k: emitted.append(event) or 0)
    now_iso = "2027-01-01T00:00:00Z"
    client.pull_request_state = lambda repo, number: {
        "state": "open", "updated_at": now_iso, "updated_ms": 1_800_000_000_000, "title": "Fix", "draft": False, "head_sha": "aaa", "branch": "beamreach/f1", "base_branch": "main",
        "mergeable_state": "clean", "review_decision": "CHANGES_REQUESTED", "auto_merge": False, "labels": [], "commits": [], "reviews": [{"state": "CHANGES_REQUESTED", "author": "alice", "bot": False, "at": now_iso}],
        "threads": [{"resolved": False, "resolved_by": None, "comment": {"id": 11, "body": "rename it", "path": "modules/storage.tf", "line": 1, "created_at": now_iso, "created_ms": 1_800_000_000_000, "author": "alice", "bot": False, "author_association": "MEMBER"}}],
        "issue_comments": [], "checks": {"state": None, "total": 0, "failed": 0, "pending": 0, "items": []},
    }
    client.labels = []
    client.add_labels = lambda repo, number, labels: client.labels.extend(labels)
    result = cap.sync({"force": True})
    assert result["checked"] == 1 and result["triggers"] == 1
    assert enqueued[0]["command"] == "address" and enqueued[0]["payload"]["review_feedback"]["comments"][0]["id"] == 11
    assert enqueued[0]["payload"]["fetch_comments"] is False
    record = cap.registry.get("acme/infra", 7)
    assert record["github"]["review_decision"] == "CHANGES_REQUESTED" and record["cursor"]["comment_ms"] == 1_800_000_000_000
    assert "pr.changes_requested" in emitted
    # Not due yet on the next pass.
    assert cap.sync({})["skipped"] == 1
    client.pull_request_state = lambda repo, number: {"state": "merged", "merged_at": now_iso, "updated_ms": 1, "commits": [], "reviews": [], "threads": [], "issue_comments": [], "checks": {}, "labels": []}
    merged = cap.sync({"repo": "acme/infra", "number": 7})
    assert merged["merged"] == 1 and cap.registry.get("acme/infra", 7)["state"] == "merged"
    assert "pr.merged" in emitted


def test_address_replies_to_questions_and_flags_contradictions(monkeypatch, setup):
    cap, client, _ = setup
    monkeypatch.setattr(captain_module.notify, "emit", lambda *a, **k: 0)
    client.labels = []
    client.add_labels = lambda repo, number, labels: client.labels.extend(labels)
    monkeypatch.setattr(captain_module.triage, "classify", lambda comments, **k: [
        {**comments[0], "kind": "question", "classified": True},
        {**comments[1], "kind": "contradicts_intent", "classified": True, "reason": "removes the fix"},
    ])
    monkeypatch.setattr(captain_module, "run_revision", lambda **k: (_ for _ in ()).throw(AssertionError("must not run")))
    client.feedback = {"comments": [_comment(cid=1, body="why this bucket?"), _comment(cid=2, body="just remove the block")], "requested_changes_by": ["alice"], "head_sha": "abc"}
    result = cap.address({"repo": "acme/infra", "number": 7, "fetch_comments": True, "trigger": "review"})
    assert result["status"] == "nothing_to_do" and result["replied"] == 2
    assert [r[0] for r in client.replies] == [1, 2]
    assert "will not make that change" in client.replies[1][1]
    record = cap.registry.get("acme/infra", 7)
    assert record["watch"]["status"] == "needs_human" and record["watch"]["reason"] == "contradicts_intent"
    assert client.labels == ["compass:needs-human"]
