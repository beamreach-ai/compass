import pytest

from compass_core import dispatch, events, queue


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(dispatch, "_registry", {})
    previous = queue._factory
    yield
    queue.set_job_queue(previous)


def test_register_and_dispatch(clean_registry):
    dispatch.register("x", {"go": lambda p: {"got": p["a"]}})
    assert dispatch.commands() == [("x", "go")]
    assert dispatch.dispatch("x", "go", {"a": 1}) == {"got": 1}


def test_unknown_command_names_the_known_ones(clean_registry):
    dispatch.register("x", {"go": lambda p: p})
    with pytest.raises(KeyError, match=r"unknown command x\.stop; known: x\.go"):
        dispatch.dispatch("x", "stop", {})


def test_inline_queue_runs_follow_up_jobs_through_the_registry(clean_registry):
    seen = []

    def first(payload):
        seen.append(("first", events.current_job_id()))
        queue.job_queue().enqueue(captain="x", command="second", payload={"workspace_id": "ws"})
        return {"ok": True}

    def second(payload):
        seen.append(("second", events.current_job_id()))
        return {"ok": True}

    dispatch.register("x", {"first": first, "second": second})
    inline = dispatch.use_inline_queue()
    assert queue.job_queue() is inline
    record = inline.enqueue(captain="x", command="first", payload={"workspace_id": "ws"})
    assert record["status"] == "completed"
    assert [name for name, _ in seen] == ["first", "second"]
    first_job, second_job = (job for _, job in seen)
    assert first_job and second_job and first_job != second_job
    assert inline.get_job(second_job)["status"] == "completed"
