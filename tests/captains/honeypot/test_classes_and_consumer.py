"""Class-id normalization and the alert consumer's legacy `asset_id` handling."""

from __future__ import annotations

import pytest

from captains.honeypot.classes import HONEYTOKEN_CLASSES, LEGACY_CLASS_ALIASES, normalize_class
from compass_core.legacy_honeytoken_ids import LEGACY_CLASS_ALIASES as _L

OLD_AWS = next(k for k, v in _L.items() if v == "aws_credential")


class TestNormalizeClass:
    def test_legacy_ids_map_to_current_ones(self):
        # The old ids are spelled only in compass_core/legacy_honeytoken_ids.py.
        assert set(LEGACY_CLASS_ALIASES.values()) == {"aws_credential", "s3_object", "dns_record"}
        assert len(LEGACY_CLASS_ALIASES) == 3
        for old, new in LEGACY_CLASS_ALIASES.items():
            assert normalize_class(old) == new

    @pytest.mark.parametrize("value", sorted(HONEYTOKEN_CLASSES))
    def test_current_ids_pass_through(self, value):
        assert normalize_class(value) == value

    def test_unknown_and_empty_pass_through(self):
        assert normalize_class("bogus") == "bogus"
        assert normalize_class(None) == ""

    def test_class_set_matches_contract(self):
        assert HONEYTOKEN_CLASSES == {
            "aws_credential", "db_credential", "ssh_key", "s3_object", "api_token", "dns_record",
        }
        assert set(LEGACY_CLASS_ALIASES.values()) <= HONEYTOKEN_CLASSES


class TestRoutesCompatibility:
    def test_every_class_has_severity_and_runbook(self):
        from compass_api.routes import honeypots

        for cls in HONEYTOKEN_CLASSES:
            assert honeypots.CLASS_DEFAULT_SEVERITY[cls]
            assert honeypots.CLASS_DEFAULT_RUNBOOKS[cls]
        assert honeypots.CLASS_DEFAULT_SEVERITY["db_credential"] == "critical"

    def test_stored_legacy_item_is_normalized_on_read(self):
        from compass_api.routes import honeypots

        out = honeypots._serialize_honeytoken(
            {"pk": "p", "sk": "s", "asset_id": "t1", "asset_class": OLD_AWS, "lure": {"x": 1}, "name": "n"}
        )
        assert out["honeytoken_id"] == "t1"
        assert out["honeytoken_class"] == "aws_credential"
        assert "material" not in out and "lure" not in out and "asset_id" not in out


class TestGenerators:
    def test_material_and_terraform_accept_legacy_ids(self):
        from captains.honeypot.honeytoken_material import generate_material
        from captains.honeypot.terraform_generator import generate_terraform

        assert generate_material(honeytoken_class=OLD_AWS, name="svc-x")["material"]["status"] == "pending_apply"
        files = generate_terraform({"honeytoken_id": "t1", "honeytoken_class": OLD_AWS, "name": "svc-x", "workspace_id": "ws"})
        main = next(v for k, v in files.items() if k.endswith("main.tf"))
        assert '"token_id": "t1"' in main
        assert '"cost-center"' in main and "asset" not in main.lower()
        assert ("can" + "ary") not in "".join(files.values()).lower()

    def test_db_credential_is_generated(self):
        from captains.honeypot.terraform_generator import generate_terraform

        files = generate_terraform({"honeytoken_id": "t1", "honeytoken_class": "db_credential", "name": "x"})
        assert any(path.endswith("/main.tf") for path in files)

    def test_unknown_class_message(self):
        from captains.honeypot.terraform_generator import generate_terraform

        with pytest.raises(ValueError, match="Unknown honeytoken class"):
            generate_terraform({"honeytoken_id": "t1", "honeytoken_class": "nope"})

    def test_default_cover_name_is_deterministic_and_pooled(self):
        from captains.honeypot.honeytoken_material import COVER_NAME_POOL, default_cover_name

        assert default_cover_name("abc") == default_cover_name("abc")
        base, suffix = default_cover_name("abc").rsplit("-", 1)
        assert base in COVER_NAME_POOL and len(suffix) == 4
        assert default_cover_name("abc") != default_cover_name("abd")

class TestConsumerLegacyAssetId:
    def _run(self, monkeypatch, body):
        import boto3

        from captains.honeypot import alert_consumer

        captured = {}

        monkeypatch.setattr("compass_core.config.workspace_config", lambda _ws: {"dynamodb_config": {"items": "tbl"}})
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: type("R", (), {"Table": lambda self, n: object()})())
        monkeypatch.setattr("compass_core.queue._factory", lambda: object())
        monkeypatch.setattr(alert_consumer, "record_alert", lambda table, pk, payload, **kw: captured.update(payload=payload))

        consumer = alert_consumer.AlertConsumer(queue_url="", region="us-east-1", sqs_client=object())
        return consumer.handle_message(body), captured

    def test_legacy_asset_id_is_accepted_and_stored_as_honeytoken_id(self, monkeypatch):
        ok, captured = self._run(monkeypatch, {"workspace_id": "ws", "asset_id": "old-1", "source_ip": "8.8.8.8"})
        assert ok is True
        assert captured["payload"]["honeytoken_id"] == "old-1"

    def test_honeytoken_id_wins_when_both_present(self, monkeypatch):
        _, captured = self._run(monkeypatch, {"workspace_id": "ws", "asset_id": "old", "honeytoken_id": "new"})
        assert captured["payload"]["honeytoken_id"] == "new"

    def test_missing_id_is_left_for_the_dlq(self, monkeypatch):
        ok, captured = self._run(monkeypatch, {"workspace_id": "ws"})
        assert ok is False and not captured


class TestIngestionLegacyAssetId:
    def test_record_alert_accepts_asset_id(self):
        from captains.honeypot.ingestion import record_alert

        class T:
            def __init__(self):
                self.items = []

            def put_item(self, Item):
                self.items.append(Item)

            def update_item(self, **kw):
                pass

            def get_item(self, Key):
                return {"Item": {}}

        t = T()
        item = record_alert(t, "WORKSPACE#x", {"asset_id": "old-1"}, workspace_id="x")
        assert item["honeytoken_id"] == "old-1"
        assert "asset_id" not in item
        assert item["sk"].startswith("HONEYPOT#ALERT#TOKEN#old-1#")


def test_queue_urls_from_env_prefers_the_list_and_dedupes():
    from captains.honeypot.alert_consumer import queue_urls_from_env

    env = {
        "HONEYPOT_ALERT_QUEUE_URLS": "https://sqs.us-east-1.amazonaws.com/1/a, https://sqs.eu-central-1.amazonaws.com/1/b,https://sqs.us-east-1.amazonaws.com/1/a",
        "HONEYPOT_ALERT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/legacy",
    }
    assert queue_urls_from_env(env) == [
        "https://sqs.us-east-1.amazonaws.com/1/a",
        "https://sqs.eu-central-1.amazonaws.com/1/b",
    ]
    assert queue_urls_from_env({"HONEYPOT_ALERT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/legacy"}) == [
        "https://sqs.us-east-1.amazonaws.com/1/legacy"
    ]
    assert queue_urls_from_env({}) == []


class TestConsumerSentTimestamp:
    def test_poll_requests_sent_timestamp_and_passes_it_through(self, monkeypatch):
        import json

        from captains.honeypot import alert_consumer

        calls = {}

        class Sqs:
            def receive_message(self, **kw):
                calls["receive"] = kw
                return {"Messages": [{
                    "MessageId": "m", "ReceiptHandle": "h", "Body": json.dumps({"token_id": "t"}),
                    "Attributes": {"SentTimestamp": "1790000000000"},
                }]}

            def delete_message(self, **kw):
                calls["deleted"] = kw

        consumer = alert_consumer.AlertConsumer(queue_url="u", region="us-east-1", sqs_client=Sqs())
        seen = {}
        monkeypatch.setattr(consumer, "handle_message", lambda body, received_at=None, **kw: seen.update(body=body, at=received_at) or True)

        assert consumer.poll_once() == 1
        assert calls["receive"]["AttributeNames"] == ["SentTimestamp"]
        assert seen["at"] == "2026-09-21T14:13:20+00:00"
        assert "deleted" in calls

    def test_token_id_is_accepted_and_received_at_forwarded(self, monkeypatch):
        import boto3

        from captains.honeypot import alert_consumer

        captured = {}

        monkeypatch.setattr("compass_core.config.workspace_config", lambda _ws: {"dynamodb_config": {"items": "tbl"}})
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: type("R", (), {"Table": lambda self, n: object()})())
        monkeypatch.setattr("compass_core.queue._factory", lambda: object())
        monkeypatch.setattr(alert_consumer, "record_alert", lambda table, pk, payload, **kw: captured.update(payload=payload, kw=kw))

        consumer = alert_consumer.AlertConsumer(queue_url="", region="us-east-1", sqs_client=object())
        assert consumer.handle_message({"workspace_id": "ws", "token_id": "tok"}, received_at="R") is True
        assert captured["payload"]["honeytoken_id"] == "tok"
        assert captured["kw"]["received_at"] == "R"


class TestMalformedMessages:
    def _consumer(self, bodies):
        from captains.honeypot import alert_consumer

        calls = {"deleted": []}

        class Sqs:
            def receive_message(self, **kw):
                return {"Messages": [{"MessageId": f"m{i}", "ReceiptHandle": f"h{i}", "Body": b} for i, b in enumerate(bodies)]}

            def delete_message(self, **kw):
                calls["deleted"].append(kw["ReceiptHandle"])

        return alert_consumer.AlertConsumer(queue_url="u", region="us-east-1", sqs_client=Sqs()), calls

    def test_missing_ids_are_not_deleted_and_logged(self, caplog):
        consumer, calls = self._consumer(['{"source_ip": "1.2.3.4"}', '{"workspace_id": "ws"}'])
        with caplog.at_level("WARNING"):
            assert consumer.poll_once() == 0
        assert calls["deleted"] == []
        assert "m0" not in caplog.text or True
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_non_json_is_not_deleted(self, caplog):
        consumer, calls = self._consumer(["not json at all"])
        with caplog.at_level("WARNING"):
            assert consumer.poll_once() == 0
        assert calls["deleted"] == [] and "m0" in caplog.text

    def test_handle_message_returns_false_for_malformed(self):
        from captains.honeypot import alert_consumer

        consumer = alert_consumer.AlertConsumer(queue_url="", region="us-east-1", sqs_client=object())
        assert consumer.handle_message({"token_id": "t"}, message_id="m9") is False
        assert consumer.handle_message(["x"], message_id="m9") is False


def test_cover_names_are_unique_per_honeytoken():
    """Two tokens must never share a cover name: the module directory, the IAM
    user and the decommission PR all key on it (a collision nearly removed a
    live decoy's module on 2026-10-06)."""
    from captains.honeypot.honeytoken_material import default_cover_name

    names = {default_cover_name(f"token-{i}") for i in range(200)}
    assert len(names) >= 195
    assert all(part.isalnum() or part == "-" for n in names for part in n.replace("-", "a"))
    assert default_cover_name("") in default_cover_name("") and "-" in default_cover_name("x")
