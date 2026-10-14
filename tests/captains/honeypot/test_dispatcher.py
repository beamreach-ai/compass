"""HP-13 / HP-28: Block Kit, Slack threads, SMTP, managed email relay."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import alert_dispatcher as ad
from captains.honeypot import incidents as inc
from compass_core.keys import notification_endpoint_sort_key, workspace_pk

WS = "ws-a"
PK = workspace_pk(WS)
CFG = {"items": "items"}
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
        yield t


def _endpoint(table, eid, **fields):
    table.put_item(Item={"pk": PK, "sk": notification_endpoint_sort_key(eid), "endpoint_id": eid, **fields})


def _incident(table):
    alert = {"alert_id": "a1", "honeytoken_id": "tok", "source_ip": "10.0.0.5", "source_task_id": "t", "triggered_at": "2026-10-06T12:00:00Z"}
    incident, _ = inc.attach_alert(table, PK, alert, placement={"service_arn": "svc"}, now_ms=T0)
    return incident["incident_id"]


def _dispatch(table, endpoint_ids, incident_id, **kw):
    return ad.dispatch_alert(
        workspace_id=WS,
        dynamodb_config=CFG,
        honeytoken_id="tok",
        honeytoken_name="prod-db-password",
        honeytoken_class="aws_credentials",
        alert_id="a1",
        source_ip="10.0.0.5",
        ip_type="internal",
        principal_arn=None,
        triggered_at="2026-10-06T12:00:00Z",
        action_config={"notification_endpoint_ids": endpoint_ids, "severity": "critical"},
        placement_note="service api / container web",
        incident_id=incident_id,
        event_time="2026-10-06T12:00:00Z",
        received_at="2026-10-06T12:00:07Z",
        **kw,
    )


class FakeHttp:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, url, payload, headers=None, timeout=10.0):
        self.calls.append((url, payload, headers or {}))
        return self.responses.pop(0) if self.responses else {}


def test_block_kit_shape():
    blocks = ad.build_alert_blocks(
        honeytoken_name="tok", honeytoken_class_label="Aws Credentials", workload="api/web",
        source_ip="1.2.3.4", ip_type="external", event_time="2026-10-06T12:00:00Z",
        received_at="2026-10-06T12:00:07Z", incident_id="inc-1",
    )
    assert [b["type"] for b in blocks] == ["header", "section", "context"]
    assert blocks[0]["text"]["text"] == "Honeytoken tripped"
    text = " ".join(f["text"] for f in blocks[1]["fields"])
    for needle in ("tok (Aws Credentials)", "api/web", "1.2.3.4 (external)", "2026-10-06T12:00:00Z", "7s"):
        assert needle in text
    assert "inc-1" in blocks[2]["elements"][0]["text"]
    repeat = ad.build_alert_blocks(
        honeytoken_name="t", honeytoken_class_label="c", workload=None, source_ip=None, ip_type=None,
        event_time=None, repeat_count=2,
    )
    assert repeat[0]["text"]["text"] == "Repeat: 2nd attempt on the same service"
    assert not any("Delivery lag" in f["text"] for f in repeat[1]["fields"])


def test_webhook_alert_has_blocks_and_text_fallback(table, monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", webhook_url="https://hooks.example/x")
    iid = _incident(table)
    res = _dispatch(table, ["e1"], iid)
    assert res["sent_at"] and res["results"][0]["ok"]
    url, payload, _ = fake.calls[0]
    assert url == "https://hooks.example/x"
    assert payload["blocks"][0]["type"] == "header" and "prod-db-password" in payload["text"]
    assert "slack_thread" not in (inc.get_incident(table, PK, iid) or {})


def test_bot_alert_stores_thread_and_updates_reply_in_it(table, monkeypatch):
    fake = FakeHttp([{"ok": True, "channel": "C123", "ts": "111.222"}, {"ok": True, "channel": "C123", "ts": "111.333"}])
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", bot_token="xoxb-secret", channel="security")
    iid = _incident(table)
    _dispatch(table, ["e1"], iid)
    url, payload, headers = fake.calls[0]
    assert url == ad.SLACK_POST_MESSAGE_URL and headers["Authorization"] == "Bearer xoxb-secret"
    assert payload["channel"] == "security" and "thread_ts" not in payload
    assert inc.get_incident(table, PK, iid)["slack_thread"] == {"channel": "C123", "ts": "111.222"}

    out = ad.post_incident_update(CFG, WS, iid, "Investigating: task abc")
    assert out["results"] == [{"endpoint_id": "e1", "ok": True}]
    assert fake.calls[1][1]["thread_ts"] == "111.222"


def test_update_without_thread_posts_new_message_and_records_it(table, monkeypatch):
    fake = FakeHttp([{"ok": True, "channel": "C9", "ts": "5.5"}])
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", bot_token="xoxb-s", channel="sec")
    iid = _incident(table)
    ad.post_incident_update(CFG, WS, iid, "hello")
    assert "thread_ts" not in fake.calls[0][1]
    assert inc.get_incident(table, PK, iid)["slack_thread"]["ts"] == "5.5"


def test_update_falls_back_to_webhook(table, monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", webhook_url="https://hooks.example/y")
    _endpoint(table, "e2", type="pagerduty", routing_key="k")
    iid = _incident(table)
    out = ad.post_incident_update(CFG, WS, iid, "Contained")
    assert [c[0] for c in fake.calls] == ["https://hooks.example/y"]
    assert fake.calls[0][1]["text"] == "Contained"
    assert len(out["results"]) == 1  # pagerduty skipped


def test_bot_error_is_reported_per_endpoint(table, monkeypatch):
    monkeypatch.setattr(ad, "_http_json", FakeHttp([{"ok": False, "error": "channel_not_found"}]))
    _endpoint(table, "e1", type="slack", bot_token="xoxb-s", channel="nope")
    res = _dispatch(table, ["e1"], _incident(table))
    assert res["sent_at"] is None and "channel_not_found" in res["results"][0]["error"]


def test_smtp_sends_text_and_html(table, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, **kw):
            sent.update(host=host, port=port, steps=[])

        def starttls(self, context=None):
            sent["steps"].append("starttls")

        def login(self, u, p):
            sent["steps"].append(("login", u, p))

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent.update(msg=msg, from_addr=from_addr, to_addrs=to_addrs)

        def quit(self):
            sent["steps"].append("quit")

    monkeypatch.setattr(ad.smtplib, "SMTP", FakeSMTP)
    _endpoint(table, "e1", type="smtp", host="mail.local", port=587, use_tls=True, username="u", password="p",
              from_address="compass@example.org", recipients=["a@example.org", "b@example.org"])
    res = _dispatch(table, ["e1"], _incident(table))
    assert res["results"][0]["ok"]
    assert sent["host"] == "mail.local" and sent["port"] == 587
    assert sent["steps"][:2] == ["starttls", ("login", "u", "p")]
    msg = sent["msg"]
    assert msg["From"] == "compass@example.org" and sent["to_addrs"] == ["a@example.org", "b@example.org"]
    assert "prod-db-password" in msg.get_body(("plain",)).get_content()
    assert "<table>" in msg.get_body(("html",)).get_content()


def test_smtp_real_server(table):
    pytest.importorskip("aiosmtpd")
    from aiosmtpd.controller import Controller

    box = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            box.append(envelope)
            return "250 OK"

    ctl = Controller(Handler(), hostname="127.0.0.1", port=0)
    ctl.start()
    try:
        _endpoint(table, "e1", type="smtp", host="127.0.0.1", port=ctl.server.sockets[0].getsockname()[1],
                  from_address="c@example.org", recipients=["a@example.org"])
        assert _dispatch(table, ["e1"], _incident(table))["results"][0]["ok"]
    finally:
        ctl.stop()
    assert box and b"prod-db-password" in box[0].content


def test_managed_email_without_any_identity_fails_cleanly(table):
    _endpoint(table, "e1", type="beamreach_email", recipients=["o@example.org"])
    res = _dispatch(table, ["e1"], _incident(table))
    assert not res["results"][0]["ok"]


WS_CONFIG = {"slack": {"team_id": "T1", "bot_token": "xoxb-workspace"}}


def test_workspace_slack_endpoint_uses_the_workspace_token_and_threads(table, monkeypatch):
    fake = FakeHttp([{"ok": True, "channel": "C42", "ts": "7.1"}, {"ok": True, "channel": "C42", "ts": "7.2"}])
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", use_workspace_slack=True, channel="C42", channel_name="sec")
    iid = _incident(table)
    res = _dispatch(table, ["e1"], iid, workspace_config=WS_CONFIG)
    assert res["results"][0]["ok"]
    url, payload, headers = fake.calls[0]
    assert url == ad.SLACK_POST_MESSAGE_URL and headers["Authorization"] == "Bearer xoxb-workspace"
    assert payload["channel"] == "C42"
    assert inc.get_incident(table, PK, iid)["slack_thread"] == {"channel": "C42", "ts": "7.1"}
    # the token is resolved per send, never written to the endpoint
    stored = table.get_item(Key={"pk": PK, "sk": notification_endpoint_sort_key("e1")})["Item"]
    assert "bot_token" not in stored

    ad.post_incident_update(CFG, WS, iid, "Contained", workspace_config=WS_CONFIG)
    assert fake.calls[1][1]["thread_ts"] == "7.1" and fake.calls[1][2]["Authorization"] == "Bearer xoxb-workspace"


def test_workspace_slack_endpoint_without_connection_fails_clearly(table, monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", use_workspace_slack=True, channel="C42")
    res = _dispatch(table, ["e1"], _incident(table), workspace_config={})
    assert res["sent_at"] is None and "not connected" in res["results"][0]["error"]
    assert fake.calls == []


def test_own_token_wins_over_workspace_token(table, monkeypatch):
    fake = FakeHttp([{"ok": True, "channel": "C1", "ts": "1.1"}])
    monkeypatch.setattr(ad, "_http_json", fake)
    _endpoint(table, "e1", type="slack", bot_token="xoxb-own", channel="sec")
    _dispatch(table, ["e1"], _incident(table), workspace_config=WS_CONFIG)
    assert fake.calls[0][2]["Authorization"] == "Bearer xoxb-own"


def test_not_in_channel_says_to_invite_the_bot(table, monkeypatch):
    monkeypatch.setattr(ad, "_http_json", FakeHttp([{"ok": False, "error": "not_in_channel"}]))
    _endpoint(table, "e1", type="slack", use_workspace_slack=True, channel="G9", is_private=True)
    res = _dispatch(table, ["e1"], _incident(table), workspace_config=WS_CONFIG)
    assert "invite @Beamreach Compass to the channel" in res["results"][0]["error"]
