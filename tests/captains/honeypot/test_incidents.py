"""HP-07: grouping alerts into incidents."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import incidents as inc
from captains.honeypot.ingestion import record_alert
from compass_core.keys import honeytoken_sort_key, workspace_pk

PK = workspace_pk("ws-a")
SERVICE = "arn:aws:ecs:us-east-1:123456789012:service/replay/web"
DAY = 24 * 3600 * 1000
T0 = 1_790_000_000_000


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for tid in ("tok-aws", "tok-db"):
            t.put_item(Item={
                "pk": PK, "sk": honeytoken_sort_key(tid), "name": tid, "honeytoken_class": "aws_credential",
                "placement": {"type": "ecs_task_env", "service_arn": SERVICE, "cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/replay"},
            })
        yield t


def alert(n, **over):
    base = {"alert_id": f"a{n}", "honeytoken_id": "tok-aws", "source_ip": "10.0.1.5", "triggered_at": "2026-10-06T12:00:00Z", "event_time": "2026-10-06T12:00:00Z"}
    base.update(over)
    return base


PLACEMENT = {"service_arn": SERVICE, "cluster_arn": "c"}


class FakeQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, **kw):
        self.enqueued.append(kw)



def _notifies(queue):
    """alert_notify jobs only; each new incident also enqueues an `investigate` job."""
    return [e for e in queue.enqueued if e.get("command") == "alert_notify"]


def test_twenty_alerts_same_task_one_incident(table):
    results = [inc.attach_alert(table, PK, alert(i, source_task_id="task-1"), placement=PLACEMENT, now_ms=T0 + i) for i in range(20)]
    assert [new for _, new in results] == [True] + [False] * 19
    got = inc.list_incidents(table, PK)
    assert len(got) == 1
    assert got[0]["alert_count"] == 20 and len(got[0]["alert_ids"]) == 20
    assert got[0]["source_task_id"] == "task-1" and got[0]["service_arn"] == SERVICE


def test_twenty_alerts_through_record_alert_notify_once(table):
    queue = FakeQueue()
    for i in range(20):
        record_alert(
            table, PK, {"honeytoken_id": "tok-aws" if i % 2 else "tok-db", "source_ip": "10.0.1.5", "task_id": "task-1"},
            workspace_id="ws-a", queue_client=queue,
        )
    assert len(_notifies(queue)) == 1
    payload = _notifies(queue)[0]["payload"]
    assert payload["incident_id"] and payload["repeat_count"] == 1 and payload["related_incident_id"] is None
    incident = inc.list_incidents(table, PK)
    assert len(incident) == 1 and incident[0]["alert_count"] == 20
    assert sorted(incident[0]["honeytoken_ids"]) == ["tok-aws", "tok-db"]
    assert len(inc.alerts_for_incident(table, PK, incident[0]["incident_id"])) == 20


def test_different_task_opens_second_incident(table):
    inc.attach_alert(table, PK, alert(1, source_task_id="task-1"), placement=PLACEMENT, now_ms=T0)
    _, new = inc.attach_alert(table, PK, alert(2, source_task_id="task-2"), placement=PLACEMENT, now_ms=T0 + 1)
    assert new and len(inc.list_incidents(table, PK)) == 2


def test_falls_back_to_ip_and_to_token_id(table):
    a, _ = inc.attach_alert(table, PK, alert(1, source_ip="1.2.3.4"), placement=None, now_ms=T0)
    assert a["grouping_key"] == "tok-aws|1.2.3.4"
    _, new = inc.attach_alert(table, PK, alert(2, source_ip="1.2.3.4"), placement=None, now_ms=T0 + 1)
    assert not new
    _, new = inc.attach_alert(table, PK, alert(3, source_ip="5.6.7.8"), placement=None, now_ms=T0 + 2)
    assert new


def test_trip_after_verified_containment_opens_linked_incident(table):
    first, _ = inc.attach_alert(table, PK, alert(1, source_task_id="task-1"), placement=PLACEMENT, now_ms=T0)
    inc.mark(table, PK, first["incident_id"], action_verified_at="2026-10-06T12:01:00Z")
    assert inc.get_incident(table, PK, first["incident_id"])["status"] == "contained"
    second, new = inc.attach_alert(table, PK, alert(2, source_task_id="task-1"), placement=PLACEMENT, now_ms=T0 + DAY)
    assert new
    assert second["related_incident_id"] == first["incident_id"] and second["repeat_count"] == 2
    third, new = inc.attach_alert(table, PK, alert(3, source_task_id="task-1"), placement=PLACEMENT, now_ms=T0 + DAY + 5)
    assert not new and third["incident_id"] == second["incident_id"]


def test_trip_during_uncontained_action_joins(table):
    first, _ = inc.attach_alert(table, PK, alert(1, source_task_id="t"), placement=PLACEMENT, now_ms=T0)
    inc.mark(table, PK, first["incident_id"], action_started_at="2026-10-06T12:00:30Z")
    _, new = inc.attach_alert(table, PK, alert(2, source_task_id="t"), placement=PLACEMENT, now_ms=T0 + 10)
    assert not new


def test_probe_is_recorded_low_and_never_opens_an_incident(table):
    queue = FakeQueue()
    item = record_alert(table, PK, {"token_id": "tok-db", "source_ip": "10.0.1.9", "kind": "probe"}, workspace_id="ws-a", queue_client=queue)
    assert item["severity"] == "low" and item["kind"] == "probe" and "incident_id" not in item
    assert inc.list_incidents(table, PK) == [] and _notifies(queue) == []
    stored = table.get_item(Key={"pk": PK, "sk": item["sk"]})["Item"]
    assert stored["severity"] == "low"
    # and a probe does not join an open incident either
    real, _ = inc.attach_alert(table, PK, alert(1, source_ip="10.0.1.9"), placement=PLACEMENT, now_ms=T0)
    probe = alert(2, source_ip="10.0.1.9", kind="probe")
    assert inc.attach_alert(table, PK, probe, placement=PLACEMENT, now_ms=T0 + 1) == (None, False)
    assert inc.get_incident(table, PK, real["incident_id"])["alert_count"] == 1


def test_seven_day_window(table):
    first, _ = inc.attach_alert(table, PK, alert(1, source_task_id="t"), placement=PLACEMENT, now_ms=T0)
    inc.mark(table, PK, first["incident_id"], action_verified_at="x")
    inside, _ = inc.attach_alert(table, PK, alert(2, source_task_id="t"), placement=PLACEMENT, now_ms=T0 + 7 * DAY)
    assert inside["related_incident_id"] == first["incident_id"]
    inc.mark(table, PK, inside["incident_id"], action_verified_at="y")
    outside, _ = inc.attach_alert(table, PK, alert(3, source_task_id="t"), placement=PLACEMENT, now_ms=T0 + 7 * DAY + 8 * DAY)
    assert "related_incident_id" not in outside and outside["repeat_count"] == 1


def test_ids_sort_newest_first_and_alert_ids_capped(table):
    ids = []
    for i in range(3):
        item, _ = inc.attach_alert(table, PK, alert(i, source_task_id=f"t{i}"), placement=PLACEMENT, now_ms=T0 + i * 1000)
        ids.append(item["incident_id"])
    assert [i["incident_id"] for i in inc.list_incidents(table, PK)] == list(reversed(ids))
    assert inc.list_incidents(table, PK, limit=2)[0]["incident_id"] == ids[-1]
    for i in range(inc.ALERT_ID_CAP + 5):
        inc.attach_alert(table, PK, alert(100 + i, source_task_id="t0"), placement=PLACEMENT, now_ms=T0 + 5000)
    got = inc.get_incident(table, PK, ids[0])
    assert len(got["alert_ids"]) == inc.ALERT_ID_CAP and got["alert_count"] == inc.ALERT_ID_CAP + 6


def test_mark_alert_sent_first_write_wins_and_mark_rejects_unknown(table):
    item, _ = inc.attach_alert(table, PK, alert(1), placement=PLACEMENT, now_ms=T0)
    inc.mark_alert_sent(table, PK, item["incident_id"], "A")
    inc.mark_alert_sent(table, PK, item["incident_id"], "B")
    assert inc.get_incident(table, PK, item["incident_id"])["first_alert_sent_at"] == "A"
    with pytest.raises(ValueError):
        inc.mark(table, PK, item["incident_id"], bogus=1)


def test_repeat_notification_carries_link(table):
    queue = FakeQueue()
    p = {"token_id": "tok-aws", "source_ip": "10.0.1.5", "task_id": "t"}
    record_alert(table, PK, p, workspace_id="ws-a", queue_client=queue)
    first = inc.list_incidents(table, PK)[0]
    inc.mark(table, PK, first["incident_id"], action_verified_at="z")
    record_alert(table, PK, p, workspace_id="ws-a", queue_client=queue)
    assert len(_notifies(queue)) == 2
    sent = _notifies(queue)[1]["payload"]
    assert sent["repeat_count"] == 2 and sent["related_incident_id"] == first["incident_id"]


def test_each_new_incident_enqueues_one_investigation(table):
    """The investigate job is enqueued once per incident, after the notification."""
    queue = FakeQueue()
    for _ in range(2):
        record_alert(table, PK, {"honeytoken_id": "tok-db", "source_ip": "10.0.1.5", "task_id": "task-1"}, workspace_id="ws-a", queue_client=queue)
    assert [e["command"] for e in queue.enqueued] == ["alert_notify", "investigate"]
    inv = [e for e in queue.enqueued if e["command"] == "investigate"][0]
    assert inv["payload"]["incident_id"] == _notifies(queue)[0]["payload"]["incident_id"]


def test_test_fire_user_agent_is_recorded_as_test_and_opens_no_incident(table):
    queue = FakeQueue()
    item = record_alert(table, PK, {"honeytoken_id": "tok-aws", "source_ip": "85.1.1.1", "user_agent": "aws-cli/2 compass-test-fire"}, workspace_id="ws-a", queue_client=queue)
    assert item.get("is_test") is True
    assert inc.list_incidents(table, PK) == [] and queue.enqueued == []


def test_every_field_the_pipeline_marks_is_markable():
    """investigate/respond/dispatch call mark() with these; an unknown field
    raises and the job fails (seen on the first real run)."""
    from captains.honeypot.incidents import MARKABLE_FIELDS

    assert {
        "correlation", "blast_radius", "investigation_done_at", "source_task_id", "cluster_arn", "service_arn",
        "action", "audit_ids", "action_chosen_at", "action_started_at", "action_verified_at",
        "summary", "report_sent_at", "slack_thread", "notified_endpoint_ids",
    } <= MARKABLE_FIELDS


def test_mark_accepts_floats_and_empty_strings(table):
    """DynamoDB rejects floats; verify timings are floats. Seen on the first
    real autopilot run, where the final write failed and the job retried."""
    inc.attach_alert(table, PK, alert(1, source_task_id="t"), placement=PLACEMENT, now_ms=T0)
    iid = inc.list_incidents(table, PK)[0]["incident_id"]
    inc.mark(table, PK, iid, action={"decisions": [{"verify": {"steady_after_s": 46.6, "new_task": ""}}]}, audit_ids=["a"])
    stored = inc.get_incident(table, PK, iid)
    assert float(stored["action"]["decisions"][0]["verify"]["steady_after_s"]) == 46.6
    assert stored["action"]["decisions"][0]["verify"]["new_task"] is None


def test_repeat_from_the_replacement_task_is_linked_by_service(table):
    """After containment the replacement task has a new id and IP; the second
    attempt must still link to the first incident (seen on the first Saturday run)."""
    first, _ = inc.attach_alert(table, PK, alert(1, source_task_id="task-old", source_ip="10.0.1.5"), placement=PLACEMENT, now_ms=T0)
    inc.mark(table, PK, first["incident_id"], action_verified_at="2026-10-06T13:49:52Z")
    second, new = inc.attach_alert(table, PK, alert(2, source_task_id="task-new", source_ip="10.0.1.9"), placement=PLACEMENT, now_ms=T0 + 120_000)
    assert new and second["incident_id"] != first["incident_id"]
    assert second["related_incident_id"] == first["incident_id"] and second["repeat_count"] == 2


def test_a_closed_incident_ends_the_repeat_chain(table):
    """Monday closes Friday and Saturday; the next trip is attempt 1 of a new
    story, not attempt 3 (seen on the first measured run, which came out as #6)."""
    first, _ = inc.attach_alert(table, PK, alert(1, source_task_id="task-a"), placement=PLACEMENT, now_ms=T0)
    inc.mark(table, PK, first["incident_id"], action_verified_at="2026-10-09T16:07:00Z")
    second, _ = inc.attach_alert(table, PK, alert(2, source_task_id="task-b"), placement=PLACEMENT, now_ms=T0 + 60_000)
    assert second["repeat_count"] == 2
    inc.mark(table, PK, second["incident_id"], action_verified_at="2026-10-09T16:09:00Z", closed_at="2026-10-09T16:10:00Z", status="closed")
    third, new = inc.attach_alert(table, PK, alert(3, source_task_id="task-c"), placement=PLACEMENT, now_ms=T0 + 120_000)
    assert new and third["repeat_count"] == 1 and "related_incident_id" not in third
