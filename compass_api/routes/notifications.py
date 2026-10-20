from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from compass_api.routes._app_config import write_guard
from compass_api.routes._workspace import get_workspace_config, get_workspace_dynamodb_config
from compass_core import sessions
from compass_core.keys import (
    notification_endpoint_prefix,
    notification_endpoint_sort_key,
    workspace_pk,
)
from compass_core.tables import dynamo_table, tables

router = APIRouter(dependencies=[Depends(write_guard())])
logger = logging.getLogger("notifications-api")

# SES sender is pre-configured by Beamreach. Users only supply recipients.
BEAMREACH_SES_SENDER = os.getenv("COMPASS_EMAIL_SENDER") or os.getenv("BEAMREACH_SES_SENDER") or "compass@localhost"


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    return value


def _resolve_table(request: Request):
    table_name = tables(get_workspace_dynamodb_config(request)).items
    if not table_name:
        raise HTTPException(status_code=500, detail="dynamodb_config.items not configured")
    return dynamo_table(table_name)


def _ws_pk(request: Request) -> str:
    from compass_api.routes._workspace import get_actor_ids
    actor = get_actor_ids(request)
    return workspace_pk(actor.get("workspace_id"))


def _serialize_endpoint(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = _to_native(item)
    raw.pop("pk", None)
    raw.pop("sk", None)
    # Never expose secret credentials to the client.
    raw.pop("webhook_url", None)
    raw.pop("routing_key", None)
    raw.pop("password", None)
    if raw.pop("bot_token", None):
        raw["bot_mode"] = True  # threads supported; the token itself never leaves the server
    return raw


def _owner_email(request: Request) -> Optional[str]:
    """The signed-in owner's address: actor context, then the OIDC header, then APP_USERNAME."""
    from compass_api.routes._workspace import get_actor_ids
    try:
        email = str((get_actor_ids(request) or {}).get("email") or "").strip()
    except Exception:  # noqa: BLE001
        email = ""
    if email:
        return email
    import base64
    oidc = request.headers.get("x-amzn-oidc-data", "")
    if oidc.count(".") >= 2:
        try:
            part = oidc.split(".")[1]
            part += "=" * (-len(part) % 4)
            email = str(json.loads(base64.urlsafe_b64decode(part)).get("email") or "").strip()
        except Exception:  # noqa: BLE001
            email = ""
    if email:
        return email
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    if "@" in admin_email:
        return admin_email
    username = os.environ.get("APP_USERNAME", "").strip()
    return username if "@" in username else None


def ensure_install_id(config: Dict[str, Any]) -> tuple[str, bool]:
    """The install's relay identity (``billing.install_id``), created lazily.

    Mutates ``config``; returns ``(install_id, created)`` so the caller can save.
    """
    billing = config.get("billing")
    if not isinstance(billing, dict):
        billing = {}
        config["billing"] = billing
    existing = str(billing.get("install_id") or "").strip()
    if existing:
        return existing, False
    billing["install_id"] = str(uuid.uuid4())
    return billing["install_id"], True


def ensure_default_email_endpoint(
    table: Any, pk: str, workspace_config: Dict[str, Any], owner_email: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Create a managed-email endpoint to the owner when the workspace has none.

    Idempotent: does nothing if any endpoint exists, if one was created before
    (``notifications.default_email_created``, so a deleted default stays deleted),
    or if the owner's address is unknown. Mutates ``workspace_config`` (the
    caller saves it) and returns the new item, else None.
    """
    if not owner_email:
        return None
    notifications_cfg = workspace_config.get("notifications")
    if not isinstance(notifications_cfg, dict):
        notifications_cfg = {}
        workspace_config["notifications"] = notifications_cfg
    if notifications_cfg.get("default_email_created"):
        return None
    existing = table.query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix()),
        Limit=1,
    ).get("Items", [])
    if existing:
        return None
    endpoint_id = str(uuid.uuid4())
    item = {
        "pk": pk,
        "sk": notification_endpoint_sort_key(endpoint_id),
        "endpoint_id": endpoint_id,
        "name": "Owner email (default)",
        "type": "beamreach_email",
        "recipients": [owner_email.strip()],
        "is_default": True,
        "created_at": _epoch_ms(),
    }
    table.put_item(Item=item)
    notifications_cfg["default_email_created"] = True
    return item


def _relay_auth(request: Request) -> tuple[Optional[str], str]:
    """(license_key or None, install_id) for the managed relay; saves a new install id."""
    config = get_workspace_config(request)
    install_id, created = ensure_install_id(config)
    if created:
        from compass_api.routes._workspace import save_config
        save_config(request, config)
    license_key = str((config.get("billing") or {}).get("license_key") or "") or None
    return license_key, install_id


# ── Models ─────────────────────────────────────────────────────────────────

class CreateEndpointPayload(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(..., pattern="^(slack|email|beamreach_email|webhook|pagerduty|smtp)$")
    webhook_url: Optional[str] = Field(default=None, description="Slack incoming webhook URL, or generic webhook target")
    channel: Optional[str] = Field(default=None, description="Slack channel name (for display)")
    recipients: Optional[List[str]] = Field(default=None, description="Email recipient addresses")
    routing_key: Optional[str] = Field(default=None, description="PagerDuty Events API v2 integration/routing key")
    # Advanced: let users bring their own SES sender
    ses_source_email: Optional[str] = Field(default=None)
    ses_region: Optional[str] = Field(default=None)
    # Slack bot mode (threads): chat.postMessage with a bot token + channel
    bot_token: Optional[str] = Field(default=None, description="Slack bot token (xoxb-…) with chat:write; stored, never returned")
    # Own SMTP server
    host: Optional[str] = Field(default=None)
    port: Optional[int] = Field(default=None)
    use_tls: bool = Field(default=False, description="STARTTLS")
    use_ssl: bool = Field(default=False, description="Implicit TLS (SMTPS)")
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None, description="Stored, never returned")
    from_address: Optional[str] = Field(default=None)


# ── CRUD ───────────────────────────────────────────────────────────────────

@router.get("/api/notifications/endpoints")
def list_endpoints(request: Request) -> List[Dict[str, Any]]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix()),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix()),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except Exception as exc:
        logger.exception("Failed to list notification endpoints")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not items:
        # Email works out of the box: a fresh workspace gets its owner as recipient.
        try:
            config = get_workspace_config(request)
            created = ensure_default_email_endpoint(table, pk, config, _owner_email(request))
            if created:
                ensure_install_id(config)
                from compass_api.routes._workspace import save_config
                save_config(request, config)
                items = [created]
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Could not create the default email endpoint")

    result = [_serialize_endpoint(i) for i in items]
    result.sort(key=lambda e: e.get("created_at", 0))
    return result


@router.post("/api/notifications/endpoints")
def create_endpoint(request: Request, body: CreateEndpointPayload) -> Dict[str, Any]:
    if body.type == "webhook" and not body.webhook_url:
        raise HTTPException(status_code=400, detail="webhook_url is required for webhook endpoints")
    if body.type == "slack":
        if body.bot_token:
            if not (body.channel or "").strip():
                raise HTTPException(status_code=400, detail="channel is required with a Slack bot token")
        elif not body.webhook_url:
            raise HTTPException(status_code=400, detail="webhook_url (or bot_token + channel) is required for slack endpoints")
    if body.type == "smtp":
        if not (body.host or "").strip():
            raise HTTPException(status_code=400, detail="host is required for smtp endpoints")
        if not (body.from_address or "").strip() or "@" not in body.from_address:
            raise HTTPException(status_code=400, detail="from_address is required for smtp endpoints")
        if body.port is not None and not (1 <= body.port <= 65535):
            raise HTTPException(status_code=400, detail="port must be between 1 and 65535")
        if body.use_tls and body.use_ssl:
            raise HTTPException(status_code=400, detail="choose either STARTTLS (use_tls) or implicit TLS (use_ssl), not both")
        if body.username and not body.password:
            raise HTTPException(status_code=400, detail="password is required when username is set")
    if body.type in ("email", "beamreach_email", "smtp") and not body.recipients:
        raise HTTPException(status_code=400, detail="recipients is required for email endpoints")
    if body.type == "pagerduty" and not body.routing_key:
        raise HTTPException(status_code=400, detail="routing_key is required for PagerDuty endpoints")

    table = _resolve_table(request)
    pk = _ws_pk(request)
    endpoint_id = str(uuid.uuid4())
    now = _epoch_ms()

    item: Dict[str, Any] = {
        "pk": pk,
        "sk": notification_endpoint_sort_key(endpoint_id),
        "endpoint_id": endpoint_id,
        "name": body.name.strip(),
        "type": body.type,
        "created_at": now,
    }

    if body.type == "slack":
        if body.webhook_url:
            item["webhook_url"] = body.webhook_url  # stored but not returned to client
        if body.bot_token:
            item["bot_token"] = body.bot_token.strip()  # stored but not returned to client
        if body.channel:
            item["channel"] = body.channel.strip().lstrip("#")
    elif body.type == "webhook":
        item["webhook_url"] = body.webhook_url  # generic JSON POST target, not returned
    elif body.type == "pagerduty":
        item["routing_key"] = body.routing_key  # PagerDuty integration key, not returned
    elif body.type == "beamreach_email":
        # Managed relay — Beamreach sends via its own verified SES; the customer
        # supplies only recipients. No SES config to store.
        item["recipients"] = [r.strip() for r in (body.recipients or []) if r.strip()]
        _relay_auth(request)  # make sure the install has its relay identity
    elif body.type == "smtp":
        item["recipients"] = [r.strip() for r in (body.recipients or []) if r.strip()]
        item["host"] = body.host.strip()
        item["port"] = body.port or (465 if body.use_ssl else 587 if body.use_tls else 25)
        item["use_tls"] = bool(body.use_tls)
        item["use_ssl"] = bool(body.use_ssl)
        item["from_address"] = body.from_address.strip()
        if body.username:
            item["username"] = body.username
            item["password"] = body.password  # stored, never returned
    elif body.type == "email":
        item["recipients"] = [r.strip() for r in (body.recipients or []) if r.strip()]
        config = get_workspace_config(request)
        # Use custom SES config if provided, otherwise fall back to Beamreach's sender
        item["ses_source_email"] = body.ses_source_email or (config.get("notifications") or {}).get("ses_source_email") or BEAMREACH_SES_SENDER
        item["ses_region"] = body.ses_region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

    try:
        table.put_item(Item=item)
    except Exception as exc:
        logger.exception("Failed to create notification endpoint")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _serialize_endpoint(item)


@router.delete("/api/notifications/endpoints/{endpoint_id}")
def delete_endpoint(request: Request, endpoint_id: str) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)
    try:
        table.delete_item(
            Key={"pk": pk, "sk": notification_endpoint_sort_key(endpoint_id)},
            ConditionExpression="attribute_exists(pk)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "deleted", "endpoint_id": endpoint_id}


@router.post("/api/notifications/endpoints/{endpoint_id}/test")
def test_endpoint(request: Request, endpoint_id: str) -> Dict[str, Any]:
    table = _resolve_table(request)
    pk = _ws_pk(request)

    resp = table.get_item(Key={"pk": pk, "sk": notification_endpoint_sort_key(endpoint_id)})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    endpoint_type = str(item.get("type", ""))

    if endpoint_type == "slack":
        test_text = "Beamreach Compass honeypot test notification: your Slack endpoint is connected."
        if item.get("bot_token"):
            from captains.honeypot.alert_dispatcher import _slack_bot_post
            try:
                _slack_bot_post(item, text=test_text, blocks=None)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Slack bot post failed: {exc}") from exc
        else:
            webhook_url = str(item.get("webhook_url", ""))
            if not webhook_url:
                raise HTTPException(status_code=400, detail="Webhook URL not stored for this endpoint")
            try:
                payload = json.dumps({"text": test_text}).encode()
                req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    if r.status not in (200, 204):
                        raise HTTPException(status_code=502, detail=f"Slack returned HTTP {r.status}")
            except urllib.error.URLError as exc:
                raise HTTPException(status_code=502, detail=f"Failed to reach Slack webhook: {exc}") from exc

    elif endpoint_type == "smtp":
        from captains.honeypot.alert_dispatcher import _send_smtp
        try:
            _send_smtp(
                item,
                subject="Beamreach Compass — honeypot notification test",
                text_body="This is a test notification from Beamreach Compass. Your SMTP endpoint is configured correctly.",
                html_body="<p>This is a test notification from <strong>Beamreach Compass</strong>. Your SMTP endpoint is configured correctly.</p>",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"SMTP send failed: {exc}") from exc

    elif endpoint_type == "webhook":
        webhook_url = str(item.get("webhook_url", ""))
        if not webhook_url:
            raise HTTPException(status_code=400, detail="Webhook URL not stored for this endpoint")
        try:
            payload = json.dumps({
                "subject": "Beamreach Compass — honeypot notification test",
                "text": "Your webhook endpoint is configured correctly.",
                "severity": "info",
            }).encode()
            req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status not in (200, 201, 202, 204):
                    raise HTTPException(status_code=502, detail=f"Webhook returned HTTP {r.status}")
        except urllib.error.URLError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to reach webhook: {exc}") from exc

    elif endpoint_type == "beamreach_email":
        recipients = list(item.get("recipients") or [])
        if not recipients:
            raise HTTPException(status_code=400, detail="No recipients configured for this endpoint")
        license_key, install_id = _relay_auth(request)
        from compass_core.notify import email_relay
        try:
            email_relay().send(
                license_key=license_key,
                install_id=install_id,
                to=recipients,
                subject="Beamreach Compass — honeypot notification test",
                text="This is a test notification sent via Beamreach managed email. Your endpoint is configured correctly.",
                api_url=os.getenv("LICENSING_API_URL"),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Managed relay failed: {exc}") from exc

    elif endpoint_type == "pagerduty":
        routing_key = str(item.get("routing_key", ""))
        if not routing_key:
            raise HTTPException(status_code=400, detail="Routing key not stored for this endpoint")
        try:
            payload = json.dumps({
                "routing_key": routing_key,
                "event_action": "trigger",
                "payload": {
                    "summary": "Beamreach Compass — honeypot notification test",
                    "source": "beamreach-honeypot",
                    "severity": "info",
                },
            }).encode()
            req = urllib.request.Request(
                "https://events.pagerduty.com/v2/enqueue",
                data=payload, headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status not in (200, 202):
                    raise HTTPException(status_code=502, detail=f"PagerDuty returned HTTP {r.status}")
        except urllib.error.URLError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to reach PagerDuty: {exc}") from exc

    elif endpoint_type == "email":
        recipients = list(item.get("recipients") or [])
        if not recipients:
            raise HTTPException(status_code=400, detail="No recipients configured for this endpoint")
        source = str(item.get("ses_source_email") or BEAMREACH_SES_SENDER)
        region = str(item.get("ses_region") or os.getenv("AWS_REGION") or "us-east-1")
        try:
            ses = sessions.control(region=region).client("ses")
            ses.send_email(
                Source=source,
                Destination={"ToAddresses": recipients},
                Message={
                    "Subject": {"Data": "Beamreach Compass — Honeypot notification test"},
                    "Body": {
                        "Text": {"Data": "This is a test notification from Beamreach Compass. Your email endpoint is configured correctly."},
                        "Html": {"Data": "<p>This is a test notification from <strong>Beamreach Compass</strong>. Your email endpoint is configured correctly.</p>"},
                    },
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to send test email: {exc}") from exc
    else:
        raise HTTPException(status_code=400, detail=f"Unknown endpoint type: {endpoint_type}")

    return {"status": "sent", "endpoint_id": endpoint_id, "type": endpoint_type}


# ── Dispatch helper (used by honeypot captain) ─────────────────────────────

def dispatch_notification(
    *,
    endpoint_item: Dict[str, Any],
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
) -> None:
    """Send a notification via the given stored endpoint item. Raises on failure."""
    endpoint_type = str(endpoint_item.get("type", ""))

    if endpoint_type == "slack":
        webhook_url = str(endpoint_item.get("webhook_url", ""))
        if not webhook_url:
            raise ValueError("webhook_url not stored")
        payload = json.dumps({"text": f"*{subject}*\n\n{text_body}"}).encode()
        req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass

    elif endpoint_type == "email":
        recipients = list(endpoint_item.get("recipients") or [])
        if not recipients:
            raise ValueError("no recipients configured")
        source = str(endpoint_item.get("ses_source_email") or BEAMREACH_SES_SENDER)
        region = str(endpoint_item.get("ses_region") or os.getenv("AWS_REGION") or "us-east-1")
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
    else:
        raise ValueError(f"Unknown endpoint type: {endpoint_type}")


# ── SES sender config ──────────────────────────────────────────────────────

class SesConfigPayload(BaseModel):
    ses_source_email: Optional[str] = Field(default=None, description="Override SES source email (must be verified in SES)")
    ses_region: Optional[str] = Field(default=None)


@router.get("/api/notifications/ses-config")
def get_ses_config(request: Request) -> Dict[str, Any]:
    config = get_workspace_config(request)
    notifications_cfg = config.get("notifications") or {}
    return {
        "ses_source_email": notifications_cfg.get("ses_source_email") or BEAMREACH_SES_SENDER,
        "ses_region": notifications_cfg.get("ses_region") or os.getenv("AWS_REGION") or "us-east-1",
        "using_beamreach_sender": not notifications_cfg.get("ses_source_email"),
    }


@router.post("/api/notifications/ses-config")
def update_ses_config(request: Request, body: SesConfigPayload) -> Dict[str, Any]:
    from compass_api.routes._workspace import save_config
    config = get_workspace_config(request)
    notifications_cfg = config.setdefault("notifications", {})
    if not isinstance(notifications_cfg, dict):
        notifications_cfg = {}
        config["notifications"] = notifications_cfg

    if body.ses_source_email:
        notifications_cfg["ses_source_email"] = body.ses_source_email.strip()
    else:
        notifications_cfg.pop("ses_source_email", None)

    if body.ses_region:
        notifications_cfg["ses_region"] = body.ses_region.strip()
    else:
        notifications_cfg.pop("ses_region", None)

    save_config(request, config)
    return get_ses_config(request)
