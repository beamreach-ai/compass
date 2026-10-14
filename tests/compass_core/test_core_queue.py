import pytest

from compass_core import events, queue


@pytest.fixture
def restore_queue():
    previous = queue._factory
    yield
    queue.set_job_queue(previous)


def test_inline_queue_runs_the_job_in_a_job_context():
    seen = {}

    def dispatch(captain, command, payload):
        seen["job_id"] = events.current_job_id()
        seen["workspace_id"] = events.current_workspace_id()
        return {"ok": True, "captain": captain, "command": command}

    q = queue.InlineQueue(dispatch)
    record = q.enqueue(captain="honeypot", command="investigate", payload={"workspace_id": "ws-1", "incident_id": "i-1"})
    assert record["status"] == "completed"
    assert record["result_summary"] == {"ok": True, "captain": "honeypot", "command": "investigate"}
    assert seen == {"job_id": record["job_id"], "workspace_id": "ws-1"}
    assert events.current_job_id() is None
    assert q.get_job(record["job_id"]) == record
    assert q.get_job(record["job_id"], workspace_id="other") is None


def test_inline_queue_records_a_failure_and_raises():
    def dispatch(captain, command, payload):
        raise ValueError("boom")

    q = queue.InlineQueue(dispatch)
    with pytest.raises(ValueError):
        q.enqueue(captain="prs", command="address", payload={})
    (record,) = q._jobs.values()
    assert record["status"] == "failed" and record["error"] == "boom"


def test_inline_queue_without_a_dispatcher_explains_itself():
    with pytest.raises(RuntimeError, match="no job queue is configured"):
        queue.InlineQueue().enqueue(captain="x", command="y", payload={})


def test_installed_factory_is_used_and_reset(restore_queue):
    class Fake:
        def enqueue(self, **kwargs):
            return {"job_id": "fake", **kwargs}

        def get_job(self, job_id, workspace_id=None):
            return None

    queue.set_job_queue(Fake)
    assert queue.job_queue().enqueue(captain="a", command="b", payload={})["job_id"] == "fake"
    queue.set_job_queue(None)
    assert isinstance(queue.job_queue(), queue.InlineQueue)
    assert queue.job_queue() is queue.job_queue(), "the inline default keeps its job records between calls"
