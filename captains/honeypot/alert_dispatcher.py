"""Dispatches alert notifications via configured endpoints."""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from compass_core import sessions
from compass_core.keys import notification_endpoint_prefix, notification_endpoint_sort_key, workspace_pk
from compass_core.tables import tables

logger = logging.getLogger("honeypot-alert-dispatcher")

BEAMREACH_SES_SENDER = os.getenv("COMPASS_EMAIL_SENDER") or os.getenv("BEAMREACH_SES_SENDER") or "compass@localhost"

SEVERITY_EMOJI = {
    "critical": "🚨",
    "high": "⚠️",
    "medium": "⚡",
    "low": "ℹ️",
}


def dispatch_alert(
    *,
    workspace_id: str,
    dynamodb_config: Dict[str, Any],
    honeytoken_id: str,
    honeytoken_name: str,
    honeytoken_class: str,
    alert_id: str,
    source_ip: Optional[str],
    ip_type: Optional[str],
    principal_arn: Optional[str],
    triggered_at: str,
    action_config: Dict[str, Any],
    runbook: Optional[str] = None,
    placement_note: Optional[str] = None,
    is_test: bool = False,
    relay_license_key: Optional[str] = None,
    relay_api_url: Optional[str] = None,
    incident_id: Optional[str] = None,
    repeat_count: Optional[int] = None,
    related_incident_id: Optional[str] = None,
    relay_install_id: Optional[str] = None,
    event_time: Optional[str] = None,
    received_at: Optional[str] = None,
    workload: Optional[str] = None,
    workspace_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send the notification. Returns ``{"sent_at": ISO|None, "results": [...]}``.

    ``sent_at`` is our clock right after the first endpoint accepted the
    message; it is also written to the incident's ``first_alert_sent_at``.
    ``workspace_config`` supplies the Slack connection (``slack.bot_token``)
    for endpoints with ``use_workspace_slack``.
    """
    result: Dict[str, Any] = {"sent_at": None, "results": []}
    table_name = tables(dynamodb_config).items
    if not table_name:
        logger.warning("No DynamoDB table configured for workspace %s", workspace_id)
        return result

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    table = sessions.control(region=region).resource("dynamodb").Table(table_name)
    pk = workspace_pk(workspace_id)

    endpoint_ids: List[str] = list(action_config.get("notification_endpoint_ids") or [])
    if not endpoint_ids:
        # A token that names no endpoints alerts every endpoint the workspace
        # has: a trip that reaches nobody is the one failure mode to avoid.
        try:
            resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix()))
            endpoint_ids = [str(i.get("endpoint_id")) for i in resp.get("Items", []) if i.get("endpoint_id")]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list notification endpoints: %s", exc)
    if not endpoint_ids:
        logger.info("No notification endpoints exist for honeytoken %s", honeytoken_id)
        return result

    # The queue payload carries the incident; fall back to reading it off the
    # stored alert so a caller that predates these kwargs still gets it.
    if not incident_id and alert_id and not is_test:
        try:
            from compass_core.keys import honeypot_alert_sort_key

            stored = table.get_item(Key={"pk": pk, "sk": honeypot_alert_sort_key(honeytoken_id, alert_id)}).get("Item") or {}
            incident_id = stored.get("incident_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not look up incident for alert %s: %s", alert_id, exc)
    if incident_id and repeat_count is None:
        try:
            from captains.honeypot.incidents import get_incident

            incident = get_incident(table, pk, incident_id) or {}
            repeat_count = int(incident.get("repeat_count", 1))
            related_incident_id = related_incident_id or incident.get("related_incident_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not look up incident %s: %s", incident_id, exc)
    repeat_note = ""
    if repeat_count and int(repeat_count) > 1:
        ordinal = {2: "2nd", 3: "3rd"}.get(int(repeat_count), f"{int(repeat_count)}th")
        repeat_note = f"REPEAT: {ordinal} attempt against the same service"
        if related_incident_id:
            repeat_note += f" (previous incident {related_incident_id})"
        repeat_note += "."

    severity = str(action_config.get("severity", "high"))
    emoji = SEVERITY_EMOJI.get(severity, "⚠️")
    effective_runbook = runbook or str(action_config.get("runbook", ""))
    honeytoken_class_label = honeytoken_class.replace("_", " ").title()
    ip_label = f"{ip_type} IP: {source_ip}" if source_ip else "unknown source"
    principal_label = f"\nPrincipal: {principal_arn}" if principal_arn else ""
    test_prefix = "[TEST] " if is_test else ""
    placement_text = f"\n\nBreach source: {placement_note}" if placement_note else ""
    placement_html = f"<p><strong>Breach source:</strong> {placement_note}</p>" if placement_note else ""

    repeat_text = f"{repeat_note}\n" if repeat_note else ""
    repeat_html = f"<p><strong>{repeat_note}</strong></p>" if repeat_note else ""
    incident_text = f"Incident: {incident_id}\n" if incident_id else ""

    subject = f"{test_prefix}{emoji} Beamreach Honeypot Alert [{severity.upper()}]: {honeytoken_name}"
    if repeat_note:
        subject += " (repeat)"
    text_body = (
        f"{'TEST FIRE — no real intrusion.' if is_test else 'HONEYPOT TRIGGERED'}: {honeytoken_name}\n"
        f"{repeat_text}{incident_text}"
        f"Honeytoken class: {honeytoken_class_label}\n"
        f"Honeytoken ID: {honeytoken_id}\n"
        f"Triggered at: {triggered_at}\n"
        f"Source: {ip_label}{principal_label}{placement_text}\n\n"
        f"--- Action runbook ---\n{effective_runbook}\n"
        f"\nManage this alert in Beamreach Compass → Honeypots."
    )
    html_body = (
        f"<h2>{test_prefix}{emoji} Honeypot Alert: {honeytoken_name}</h2>"
        f"{'<p><em>This is a test fire — no real intrusion.</em></p>' if is_test else ''}"
        f"{repeat_html}"
        f"<table>"
        f"<tr><th>Honeytoken class</th><td>{honeytoken_class_label}</td></tr>"
        f"<tr><th>Severity</th><td><strong>{severity.upper()}</strong></td></tr>"
        f"<tr><th>Honeytoken ID</th><td><code>{honeytoken_id}</code></td></tr>"
        f"<tr><th>Triggered at</th><td>{triggered_at}</td></tr>"
        f"<tr><th>Source</th><td>{ip_label}{principal_label}</td></tr>"
        f"</table>"
        f"{placement_html}"
        f"<h3>Action runbook</h3><pre>{effective_runbook}</pre>"
        f"<p>Manage this alert in Compass → Honeypots.</p>"
    )

    blocks = build_alert_blocks(
        honeytoken_name=honeytoken_name,
        honeytoken_class_label=honeytoken_class_label,
        workload=workload or placement_note or None,
        source_ip=source_ip,
        ip_type=ip_type,
        event_time=event_time or triggered_at,
        received_at=received_at,
        incident_id=incident_id,
        repeat_count=repeat_count,
        severity=severity,
        is_test=is_test,
    )

    notified: List[str] = []
    for endpoint_id in endpoint_ids:
        try:
            resp = table.get_item(Key={"pk": pk, "sk": notification_endpoint_sort_key(endpoint_id)})
            endpoint = resp.get("Item")
            if not endpoint:
                logger.warning("Endpoint %s not found for workspace %s", endpoint_id, workspace_id)
                result["results"].append({"endpoint_id": endpoint_id, "ok": False, "error": "endpoint not found"})
                continue
            endpoint = resolve_endpoint(endpoint, workspace_config)
            sent = _send(
                endpoint,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                severity=severity,
                blocks=blocks,
                relay_license_key=relay_license_key,
                relay_install_id=relay_install_id,
                relay_api_url=relay_api_url,
            )
            notified.append(endpoint_id)
            if sent and sent.get("slack_thread") and incident_id and not is_test:
                _store_slack_thread(table, pk, incident_id, sent["slack_thread"])
            sent_at = datetime.now(timezone.utc).isoformat()
            result["results"].append({"endpoint_id": endpoint_id, "ok": True, "sent_at": sent_at})
            if result["sent_at"] is None:
                result["sent_at"] = sent_at
                if incident_id:
                    try:
                        from captains.honeypot.incidents import mark_alert_sent

                        mark_alert_sent(table, pk, incident_id, sent_at)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Could not record first_alert_sent_at on %s: %s", incident_id, exc)
        except Exception as exc:
            logger.exception("Failed to dispatch alert to endpoint %s: %s", endpoint_id, exc)
            result["results"].append({"endpoint_id": endpoint_id, "ok": False, "error": str(exc)})
    if notified and incident_id and not is_test:
        _store_notified_endpoints(table, pk, incident_id, notified)
    return result


# ── Slack Block Kit ────────────────────────────────────────────────────────

def _mrkdwn(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def delivery_lag_seconds(event_time: Optional[str], received_at: Optional[str]) -> Optional[float]:
    a, b = _parse_ts(event_time), _parse_ts(received_at)
    if not a or not b:
        return None
    return max(0.0, (b - a).total_seconds())


def _format_lag(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f} min"


def build_alert_blocks(
    *,
    honeytoken_name: str,
    honeytoken_class_label: str,
    workload: Optional[str],
    source_ip: Optional[str],
    ip_type: Optional[str],
    event_time: Optional[str],
    received_at: Optional[str] = None,
    incident_id: Optional[str] = None,
    repeat_count: Optional[int] = None,
    severity: str = "high",
    is_test: bool = False,
) -> List[Dict[str, Any]]:
    """Block Kit body of the first message of an incident."""
    if repeat_count and int(repeat_count) > 1:
        ordinal = {2: "2nd", 3: "3rd"}.get(int(repeat_count), f"{int(repeat_count)}th")
        title = f"Repeat: {ordinal} attempt on the same service"
    else:
        title = "Honeytoken tripped"
    if is_test:
        title = f"[TEST] {title}"
    ip_text = "unknown"
    if source_ip:
        ip_text = f"{source_ip} ({ip_type})" if ip_type and ip_type != "unknown" else str(source_ip)
    fields = [
        {"type": "mrkdwn", "text": f"*Token*\n{_mrkdwn(honeytoken_name)} ({_mrkdwn(honeytoken_class_label)})"},
        {"type": "mrkdwn", "text": f"*Workload*\n{_mrkdwn(workload) if workload else 'not recorded'}"},
        {"type": "mrkdwn", "text": f"*Source IP*\n{_mrkdwn(ip_text)}"},
        {"type": "mrkdwn", "text": f"*Event time*\n{_mrkdwn(event_time or 'unknown')}"},
    ]
    lag = delivery_lag_seconds(event_time, received_at)
    if lag is not None:
        fields.append({"type": "mrkdwn", "text": f"*Delivery lag*\n{_format_lag(lag)}"})
    context = f"Incident `{_mrkdwn(incident_id)}` | severity {severity}" if incident_id else f"severity {severity}"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {"type": "section", "fields": fields},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]


SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_BOT_MENTION = "@Beamreach Compass"


def resolve_endpoint(endpoint: Dict[str, Any], workspace_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The endpoint ready to send: a ``use_workspace_slack`` Slack endpoint without
    its own token gets the workspace connection's bot token (a copy; nothing is stored).

    Raises ValueError when such an endpoint has no connected workspace to post with.
    """
    if endpoint.get("type") != "slack" or not endpoint.get("use_workspace_slack") or endpoint.get("bot_token"):
        return endpoint
    slack = (workspace_config or {}).get("slack")
    token = str(slack.get("bot_token") or "") if isinstance(slack, dict) else ""
    if not token:
        raise ValueError("Slack is not connected for this workspace; connect it in Settings → Slack")
    return {**endpoint, "bot_token": token}


def _http_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    """POST JSON, return the parsed JSON body ({} when empty or not JSON)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read() or b""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _slack_bot_post(
    endpoint: Dict[str, Any], *, text: str, blocks: Optional[List[Dict[str, Any]]], thread_ts: Optional[str] = None
) -> Dict[str, str]:
    """chat.postMessage with the endpoint's bot token. Returns {channel, ts}."""
    payload: Dict[str, Any] = {"channel": endpoint.get("channel"), "text": text}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = _http_json(SLACK_POST_MESSAGE_URL, payload, {"Authorization": f"Bearer {endpoint['bot_token']}"})
    if not data.get("ok"):
        error = str(data.get("error") or "no response")
        hint = ""
        if error == "not_in_channel":
            hint = f" (invite {SLACK_BOT_MENTION} to the channel)"
        elif error == "channel_not_found" and endpoint.get("use_workspace_slack"):
            hint = f" (if the channel is private, invite {SLACK_BOT_MENTION} to the channel)"
        raise RuntimeError(f"Slack chat.postMessage failed: {error}{hint}")
    return {"channel": str(data.get("channel") or endpoint.get("channel")), "ts": str(data.get("ts"))}


def _store_slack_thread(table: Any, pk: str, incident_id: str, thread: Dict[str, str]) -> None:
    try:
        from captains.honeypot.incidents import mark

        mark(table, pk, incident_id, slack_thread=thread)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record slack_thread on %s: %s", incident_id, exc)


def _store_notified_endpoints(table: Any, pk: str, incident_id: str, endpoint_ids: List[str]) -> None:
    """Remember who got the alert so later updates go to the same endpoints."""
    try:
        from compass_core.keys import honeypot_incident_sort_key

        table.update_item(
            Key={"pk": pk, "sk": honeypot_incident_sort_key(incident_id)},
            UpdateExpression="SET notified_endpoint_ids = :e",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":e": endpoint_ids},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record notified endpoints on %s: %s", incident_id, exc)


def post_incident_update(
    dynamodb_config: Dict[str, Any],
    workspace_id: str,
    incident_id: str,
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
    *,
    subject: Optional[str] = None,
    relay_license_key: Optional[str] = None,
    relay_install_id: Optional[str] = None,
    relay_api_url: Optional[str] = None,
    workspace_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Post a status update about an incident to the endpoints that got its alert.

    Slack: a reply in the incident's thread when one exists (bot-token endpoints),
    otherwise a new message (bot token, whose ts becomes the thread) or the plain
    webhook. Email-type endpoints get one mail per update. Other types are skipped.
    Returns ``{"results": [...]}``; never raises for a single endpoint's failure.
    """
    out: Dict[str, Any] = {"results": []}
    table_name = tables(dynamodb_config).items
    if not table_name:
        return out
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    table = sessions.control(region=region).resource("dynamodb").Table(table_name)
    pk = workspace_pk(workspace_id)

    from compass_core.keys import honeypot_incident_sort_key

    incident = (table.get_item(Key={"pk": pk, "sk": honeypot_incident_sort_key(incident_id)}) or {}).get("Item") or {}
    thread = incident.get("slack_thread") or None
    ids = list(incident.get("notified_endpoint_ids") or [])
    if ids:
        endpoints = []
        for eid in ids:
            item = (table.get_item(Key={"pk": pk, "sk": notification_endpoint_sort_key(eid)}) or {}).get("Item")
            if item:
                endpoints.append(item)
    else:
        resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix()))
        endpoints = list(resp.get("Items", []))

    mail_subject = subject or f"Beamreach Honeypot update: incident {incident_id}"
    html = "<p>" + _mrkdwn(text).replace("\n", "<br>") + "</p>"
    for endpoint in endpoints:
        etype = str(endpoint.get("type", ""))
        eid = endpoint.get("endpoint_id")
        try:
            if etype == "slack":
                endpoint = resolve_endpoint(endpoint, workspace_config)
                if endpoint.get("bot_token"):
                    same_channel = thread and _same_channel(thread, endpoint)
                    posted = _slack_bot_post(
                        endpoint, text=text, blocks=blocks, thread_ts=thread["ts"] if same_channel else None
                    )
                    if not thread:
                        thread = posted
                        _store_slack_thread(table, pk, incident_id, posted)
                else:
                    _slack_webhook_post(endpoint, text=text, blocks=blocks)
            elif etype in ("email", "beamreach_email", "smtp"):
                _send(
                    endpoint,
                    subject=mail_subject,
                    text_body=text,
                    html_body=html,
                    relay_license_key=relay_license_key,
                    relay_install_id=relay_install_id,
                    relay_api_url=relay_api_url,
                )
            else:
                continue
            out["results"].append({"endpoint_id": eid, "ok": True})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Incident update to endpoint %s failed: %s", eid, exc)
            out["results"].append({"endpoint_id": eid, "ok": False, "error": str(exc)})
    return out


def _same_channel(thread: Dict[str, Any], endpoint: Dict[str, Any]) -> bool:
    configured = str(endpoint.get("channel") or "").lstrip("#")
    posted = str(thread.get("channel") or "").lstrip("#")
    # chat.postMessage returns a channel id even when configured with a name, so a
    # mismatch of spelling is not a different channel; only one bot endpoint is
    # expected per workspace, so treat any thread as ours unless both look alike.
    return not configured or not posted or configured == posted or posted.startswith(("C", "G", "D"))


def _slack_webhook_post(endpoint: Dict[str, Any], *, text: str, blocks: Optional[List[Dict[str, Any]]]) -> None:
    webhook_url = str(endpoint.get("webhook_url", ""))
    if not webhook_url:
        raise ValueError("webhook_url not stored for endpoint")
    payload: Dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    _http_json(webhook_url, payload)


def _send_smtp(endpoint: Dict[str, Any], *, subject: str, text_body: str, html_body: Optional[str]) -> None:
    recipients = list(endpoint.get("recipients") or [])
    host = str(endpoint.get("host") or "")
    if not recipients or not host:
        raise ValueError("smtp endpoint needs host and recipients")
    from_address = str(endpoint.get("from_address") or endpoint.get("username") or "")
    if not from_address:
        raise ValueError("smtp endpoint needs from_address")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    port = int(endpoint.get("port") or (465 if endpoint.get("use_ssl") else 587 if endpoint.get("use_tls") else 25))
    if endpoint.get("use_ssl"):
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, port, timeout=15)
    try:
        if endpoint.get("use_tls") and not endpoint.get("use_ssl"):
            client.starttls(context=ssl.create_default_context())
        if endpoint.get("username"):
            client.login(str(endpoint["username"]), str(endpoint.get("password") or ""))
        client.send_message(msg, from_addr=from_address, to_addrs=recipients)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass


# PagerDuty Events API v2 only accepts these severities.
_PAGERDUTY_SEVERITY = {"critical": "critical", "high": "error", "medium": "warning", "low": "info"}


def _send(
    endpoint: Dict[str, Any],
    *,
    subject: str,
    text_body: str,
    html_body: Optional[str],
    severity: str = "high",
    blocks: Optional[List[Dict[str, Any]]] = None,
    relay_license_key: Optional[str] = None,
    relay_install_id: Optional[str] = None,
    relay_api_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Deliver one message. Returns ``{"slack_thread": {...}}`` for bot-token Slack."""
    endpoint_type = str(endpoint.get("type", ""))

    if endpoint_type == "beamreach_email":
        from compass_core.notify import email_relay

        recipients = list(endpoint.get("recipients") or [])
        if not recipients:
            raise ValueError("no recipients configured")
        if not relay_license_key and not relay_install_id:
            raise ValueError("managed email needs a license key or install id")
        email_relay().send(
            license_key=relay_license_key,
            install_id=relay_install_id,
            to=recipients,
            subject=subject,
            text=text_body,
            html=html_body,
            api_url=relay_api_url,
        )
        logger.info("Managed relay alert sent to %s", recipients)
        return None

    if endpoint_type == "smtp":
        _send_smtp(endpoint, subject=subject, text_body=text_body, html_body=html_body)
        logger.info("SMTP alert sent to %s", endpoint.get("recipients"))
        return None

    if endpoint_type == "webhook":
        target = str(endpoint.get("webhook_url", ""))
        if not target:
            raise ValueError("webhook_url not stored for endpoint")
        payload = json.dumps({"subject": subject, "text": text_body, "severity": severity}).encode()
        req = urllib.request.Request(target, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
        logger.info("Webhook alert sent to %s", endpoint.get("endpoint_id"))

    elif endpoint_type == "pagerduty":
        routing_key = str(endpoint.get("routing_key", ""))
        if not routing_key:
            raise ValueError("routing_key not stored for endpoint")
        pd_payload = json.dumps({
            "routing_key": routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": subject,
                "source": "beamreach-honeypot",
                "severity": _PAGERDUTY_SEVERITY.get(severity, "error"),
                "custom_details": {"body": text_body},
            },
        }).encode()
        req = urllib.request.Request(
            "https://events.pagerduty.com/v2/enqueue",
            data=pd_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        logger.info("PagerDuty alert sent for %s", endpoint.get("endpoint_id"))

    elif endpoint_type == "slack":
        fallback = f"{subject}\n\n{text_body}"
        if endpoint.get("bot_token"):
            thread = _slack_bot_post(endpoint, text=fallback, blocks=blocks)
            logger.info("Slack bot alert sent to %s", endpoint.get("endpoint_id"))
            return {"slack_thread": thread}
        _slack_webhook_post(endpoint, text=fallback, blocks=blocks)
        logger.info("Slack alert sent to %s", endpoint.get("endpoint_id"))

    elif endpoint_type == "email":
        recipients = list(endpoint.get("recipients") or [])
        if not recipients:
            raise ValueError("no recipients configured")
        source = str(endpoint.get("ses_source_email") or BEAMREACH_SES_SENDER)
        region = str(endpoint.get("ses_region") or os.getenv("AWS_REGION") or "us-east-1")
        ses = sessions.control(region=region).client("ses")
        body_parts: Dict[str, Any] = {"Text": {"Data": text_body}}
        if html_body:
            body_parts["Html"] = {"Data": html_body}
        ses.send_email(
            Source=source,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject},
                "Body": body_parts,
            },
        )
        logger.info("Email alert sent to %s via SES", recipients)
    else:
        raise ValueError(f"Unknown endpoint type: {endpoint_type}")
    return None
