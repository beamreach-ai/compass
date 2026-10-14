"""Tests for honeypot alert ingestion.

Alerts arrive over SQS from a cross-account EventBridge rule; the HTTP endpoint
is a secondary path for testing. Both call `record_alert`, so these cover the
behaviour both depend on.
"""

from __future__ import annotations

import pytest

from captains.honeypot.ingestion import classify_ip, record_alert


class FakeTable:
    def __init__(self, honeytoken=None):
        self.put_items = []
        self.updates = []
        self._honeytoken = honeytoken or {"name": "svc-backup-reporting", "honeytoken_class": "aws_credential"}

    def put_item(self, Item):
        self.put_items.append(Item)

    def update_item(self, **kwargs):
        self.updates.append(kwargs)

    def get_item(self, Key):
        return {"Item": self._honeytoken}


class FakeQueue:
    def __init__(self, fail=False):
        self.enqueued = []
        self.fail = fail

    def enqueue(self, **kwargs):
        if self.fail:
            raise RuntimeError("broker unreachable")
        self.enqueued.append(kwargs)



def _notifies(queue):
    """alert_notify jobs only; each new incident also enqueues an `investigate` job."""
    return [e for e in queue.enqueued if e.get("command") == "alert_notify"]


class TestClassifyIp:
    @pytest.mark.parametrize("addr", ["10.0.1.5", "172.16.0.1", "192.168.1.1", "127.0.0.1"])
    def test_rfc1918_and_loopback_are_internal(self, addr):
        assert classify_ip(addr) == "internal"

    @pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "45.33.32.156"])
    def test_public_addresses_are_external(self, addr):
        assert classify_ip(addr) == "external"

    @pytest.mark.parametrize("addr", ["203.0.113.7", "192.0.2.1", "198.51.100.5"])
    def test_documentation_ranges_are_external_not_internal(self, addr):
        """`ipaddress.is_private` is True for these, which is why it isn't used.

        203.0.113.7 is the address the test-fire path sends. Reporting it as an
        internal source would tell a responder the call came from inside the
        network — the opposite of the truth.
        """
        assert classify_ip(addr) == "external"

    def test_link_local_is_external(self):
        """169.254.0.0/16 is also `is_private`, and also not our network."""
        assert classify_ip("169.254.169.254") == "external"

    def test_aws_service_principals_are_recognised(self):
        assert classify_ip("s3.amazonaws.com") == "aws_service"

    @pytest.mark.parametrize("value", [None, "", "   ", "not-an-address"])
    def test_undeterminable_returns_none(self, value):
        assert classify_ip(value) is None


class TestRecordAlert:
    def _payload(self, **over):
        base = {"honeytoken_id": "token-1", "source_ip": "45.33.32.156", "service": "s3"}
        base.update(over)
        return base

    def test_stores_the_alert(self):
        table = FakeTable()

        item = record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo")

        assert len(table.put_items) == 1
        assert item["honeytoken_id"] == "token-1"
        assert item["alert_id"]
        assert item["triggered_at"]

    def test_bumps_the_asset_counters(self):
        table = FakeTable()

        record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo")

        assert len(table.updates) == 1
        assert "trigger_count" in table.updates[0]["UpdateExpression"]

    def test_derives_ip_type_when_absent(self):
        """EventBridge input transformers cannot classify an address, so a real
        honeytoken trip always arrives without one."""
        table = FakeTable()

        item = record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo")

        assert item["ip_type"] == "external"

    def test_respects_an_explicit_ip_type(self):
        table = FakeTable()

        item = record_alert(
            table, "WORKSPACE#demo", self._payload(ip_type="internal"), workspace_id="demo"
        )

        assert item["ip_type"] == "internal"

    def test_marks_test_alerts(self):
        table = FakeTable()

        item = record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo", is_test=True)

        assert item["is_test"] is True

    def test_real_alerts_carry_no_test_flag(self):
        """The UI badges on presence of the key, so it must be absent entirely."""
        table = FakeTable()

        item = record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo")

        assert "is_test" not in item

    def test_omits_fields_that_were_not_supplied(self):
        table = FakeTable()

        item = record_alert(table, "WORKSPACE#demo", {"honeytoken_id": "token-1"}, workspace_id="demo")

        assert "user_agent" not in item
        assert "principal_arn" not in item

    def test_enqueues_notification(self):
        table, queue = FakeTable(), FakeQueue()

        record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo", queue_client=queue)

        assert len(_notifies(queue)) == 1
        assert _notifies(queue)[0]["command"] == "alert_notify"
        assert _notifies(queue)[0]["payload"]["honeytoken_name"] == "svc-backup-reporting"

    def test_notifies_once_per_incident_not_per_alert(self):
        table, queue = FakeTable(), FakeQueue()
        incidents = {}

        def attach(tbl, pk, alert, *, placement, now_ms):
            new = not incidents
            incidents.setdefault("i", {"incident_id": "1-abc", "repeat_count": 1})
            return incidents["i"], new

        import captains.honeypot.incidents as mod

        orig = mod.attach_alert
        mod.attach_alert = attach
        try:
            for _ in range(3):
                record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo", queue_client=queue)
        finally:
            mod.attach_alert = orig
        assert len(_notifies(queue)) == 1 and _notifies(queue)[0]["payload"]["incident_id"] == "1-abc"
        assert len(table.put_items) == 3

    def test_notification_failure_does_not_lose_the_alert(self):
        """The alert is durable before notification is attempted.

        Raising here would fail the SQS receipt, the message would redeliver,
        and the same trip would be recorded twice — while the operator still
        would not have been told.
        """
        table, queue = FakeTable(), FakeQueue(fail=True)

        item = record_alert(table, "WORKSPACE#demo", self._payload(), workspace_id="demo", queue_client=queue)

        assert len(table.put_items) == 1
        assert item["alert_id"]

    def test_rejects_a_payload_with_no_honeytoken(self):
        with pytest.raises(ValueError, match="honeytoken_id is required"):
            record_alert(FakeTable(), "WORKSPACE#demo", {"source_ip": "8.8.8.8"}, workspace_id="demo")


class TestTimestampsAndEventFields:
    def test_three_timestamps_are_stored_separately(self):
        table = FakeTable()
        payload = {
            "token_id": "token-1",
            "event_time": "2026-10-06T12:00:00Z",
            "source_ip": "45.33.32.156",
        }

        item = record_alert(
            table, "WORKSPACE#demo", payload, workspace_id="demo", received_at="2026-10-06T12:00:03+00:00"
        )

        assert item["honeytoken_id"] == "token-1"
        assert item["event_time"] == "2026-10-06T12:00:00Z"
        assert item["received_at"] == "2026-10-06T12:00:03+00:00"
        assert item["processed_at"] not in (item["event_time"], item["received_at"])
        assert item["triggered_at"] == item["event_time"]
        assert table.updates[0]["ExpressionAttributeValues"][":t"] == "2026-10-06T12:00:00Z"

    def test_triggered_at_falls_back_to_processed_at(self):
        item = record_alert(FakeTable(), "WORKSPACE#demo", {"honeytoken_id": "t"}, workspace_id="demo")

        assert "event_time" not in item and "received_at" not in item
        assert item["triggered_at"] == item["processed_at"]

    def test_event_fields_are_stored_and_empty_strings_are_missing(self):
        payload = {
            "token_id": "token-1",
            "event_id": "e-1",
            "event_name": "GetCallerIdentity",
            "region": "eu-west-1",
            "access_key_id": "AKIAEXAMPLE",
            "principal_arn": "arn:aws:iam::1:user/svc",
            "error_code": "",
            "vpc_endpoint_id": "",
            "user_agent": "",
            "event_time": "",
            "source_ip": "8.8.8.8",
        }

        item = record_alert(FakeTable(), "WORKSPACE#demo", payload, workspace_id="demo")

        for key, value in (
            ("event_id", "e-1"),
            ("event_name", "GetCallerIdentity"),
            ("region", "eu-west-1"),
            ("access_key_id", "AKIAEXAMPLE"),
            ("principal_arn", "arn:aws:iam::1:user/svc"),
        ):
            assert item[key] == value
        for key in ("error_code", "vpc_endpoint_id", "user_agent", "event_time"):
            assert key not in item
        assert item["triggered_at"] == item["processed_at"]

    def test_notification_carries_the_timestamps(self):
        table, queue = FakeTable(), FakeQueue()

        record_alert(
            table, "WORKSPACE#demo", {"token_id": "t", "event_time": "2026-10-06T12:00:00Z"},
            workspace_id="demo", queue_client=queue, received_at="r",
        )

        sent = _notifies(queue)[0]["payload"]
        assert sent["event_time"] == "2026-10-06T12:00:00Z" and sent["received_at"] == "r"
        assert sent["triggered_at"] == "2026-10-06T12:00:00Z" and sent["processed_at"]
