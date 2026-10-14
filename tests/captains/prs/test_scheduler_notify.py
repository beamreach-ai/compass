from __future__ import annotations

from captains.prs import notify, scheduler


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return {"job_id": f"j{len(self.calls)}", "status": "queued" if len(self.calls) == 1 else "running"}


def test_tick_enqueues_one_sync_per_workspace_with_dedupe(monkeypatch):
    queue = FakeQueue()
    assert scheduler.tick(["ws-a", "ws-b"], client=queue) == 1
    assert [c["payload"]["workspace_id"] for c in queue.calls] == ["ws-a", "ws-b"]
    assert all(c["session_id"] == "prs-sync" and c["enforce_single_active_session"] for c in queue.calls)
    monkeypatch.setenv("PRS_TICK_SECONDS", "0")
    assert scheduler.start_ticker() is None
    monkeypatch.delenv("WORKSPACES_TABLE", raising=False)
    monkeypatch.setenv("DEFAULT_WORKSPACE", "ws-default")
    assert scheduler.workspace_ids() == ["ws-default"]


def test_emit_routes_to_configured_endpoints(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_load_endpoints", lambda config, ws, ids: [{"endpoint_id": i, "type": "slack"} for i in ids])
    config = {"notifications": {"routing": {"pr.needs_human": ["ep-1", "ep-2"], "pr.*": ["ep-3"]}}}

    def sender(endpoint, **kwargs):
        sent.append((endpoint["endpoint_id"], kwargs["subject"], kwargs["severity"]))

    assert notify.emit(config, "ws", "pr.needs_human", {"repo": "a/b", "number": 1, "reason": "scope_expanded"}, sender=sender) == 2
    assert sent[0][0] == "ep-1" and "scope expanded" in sent[0][1] and sent[0][2] == "high"
    assert notify.emit(config, "ws", "pr.merged", {"repo": "a/b", "number": 1}, sender=sender) == 1
    assert sent[-1][0] == "ep-3"
    assert notify.emit({}, "ws", "pr.ready", {"repo": "a/b", "number": 1}, sender=sender) == 0
