import pytest

from compass_core import events


class RecordingSink:
    def __init__(self):
        self.events = []

    def append_event(self, job_id, message, *, event_type, status, data, workspace_id):
        self.events.append((job_id, message, event_type, status, data, workspace_id))


@pytest.fixture
def sink():
    previous = events.event_sink()
    recording = RecordingSink()
    events.set_event_sink(recording)
    yield recording
    events.set_event_sink(previous)


def test_events_outside_a_job_context_are_dropped(sink):
    events.emit_job_event("nothing to attach to")
    assert sink.events == []


def test_events_inside_a_job_context_carry_job_and_workspace(sink):
    with events.job_event_context("job-1", "ws-1"):
        assert events.current_job_id() == "job-1"
        assert events.current_workspace_id() == "ws-1"
        events.emit_job_event("step", event_type="progress", status="running", data={"n": 1})
    assert sink.events == [("job-1", "step", "progress", "running", {"n": 1}, "ws-1")]
    assert events.current_job_id() is None


def test_a_failing_sink_never_breaks_the_caller(sink):
    class Broken:
        def append_event(self, *a, **k):
            raise RuntimeError("down")

    events.set_event_sink(Broken())
    with events.job_event_context("job-2"):
        events.emit_job_event("still fine")


def test_log_sink_is_the_default_before_anything_is_installed(caplog):
    previous = events.event_sink()
    events.set_event_sink(events.LogSink())
    try:
        with caplog.at_level("INFO", logger="compass.events"), events.job_event_context("job-3"):
            events.emit_job_event("logged", status="completed")
        assert "job job-3: logged [completed]" in caplog.text
    finally:
        events.set_event_sink(previous)
