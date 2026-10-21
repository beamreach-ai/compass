"""Slack as a workspace-level integration (Settings → Slack), via Slack OAuth v2.

Connecting installs the Beamreach Compass app into one Slack workspace and
stores the bot token in the workspace config (``slack``), which is backed by
Secrets Manager. No channel is picked at install time: alert channels are
chosen afterwards (Honeypots → Settings → Alert channels), each one a slack
notification endpoint with ``use_workspace_slack`` whose token the dispatcher
resolves from the workspace config at send time. The token never leaves the
server.

Flow: ``/install`` redirects to Slack with a signed ``state``; Slack sends the
browser back to ``/callback`` with a ``code``, which is exchanged for a bot
token. The callback then redirects to the Settings tab with
``slack=connected`` or ``slack=error&reason=…``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from compass_api.routes import notifications as _notifications
from compass_api.routes._app_config import ensure_not_read_only, write_guard
from compass_api.routes._workspace import get_actor_ids, get_workspace_config, save_config
from compass_core import sessions
from compass_core.keys import notification_endpoint_prefix, notification_endpoint_sort_key

router = APIRouter(dependencies=[Depends(write_guard())])
logger = logging.getLogger("slack-connect")

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
SLACK_CONVERSATIONS_LIST_URL = "https://slack.com/api/conversations.list"
SLACK_AUTH_REVOKE_URL = "https://slack.com/api/auth.revoke"
SLACK_SCOPES = "chat:write,chat:write.public,channels:read,groups:read"
BASE_PATH = "/api/integrations/slack"
CALLBACK_PATH = f"{BASE_PATH}/callback"
RETURN_PATH = "/workspace?tab=settings"
STATE_TTL_SECONDS = 600
CHANNELS_CAP = 1000
TEST_MESSAGE = "Beamreach Compass test notification: alerts and incident updates will appear in this channel."
# Slack errors meaning the stored token no longer works: the user must reconnect.
_DEAD_TOKEN_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired"}

_CACHE_HIT_TTL = 300.0
_CACHE_MISS_TTL = 60.0
_app_cache: Dict[str, Any] = {"at": 0.0, "value": None, "loaded": False}


# ── App credentials ────────────────────────────────────────────────────────

def _read_slack_app() -> Optional[Dict[str, str]]:
    client_id = (os.getenv("SLACK_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("SLACK_CLIENT_SECRET") or "").strip()
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    secret_id = (os.getenv("SLACK_APP_SECRET_ID") or "beamreach/slack-app").strip()
    region = os.getenv("AWS_REGION") or "eu-central-1"
    try:
        raw = sessions.control(region=region).client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Slack app secret %s not readable: %s", secret_id, type(exc).__name__)
        return None
    client_id = str((data or {}).get("client_id") or "").strip()
    client_secret = str((data or {}).get("client_secret") or "").strip()
    if not client_id or not client_secret:
        logger.debug("Slack app secret %s lacks client_id/client_secret", secret_id)
        return None
    return {"client_id": client_id, "client_secret": client_secret}


def load_slack_app() -> Optional[Dict[str, str]]:
    """``{client_id, client_secret}`` of the Compass Slack app, or None when not configured.

    Env ``SLACK_CLIENT_ID``/``SLACK_CLIENT_SECRET`` first, else the Secrets
    Manager secret ``SLACK_APP_SECRET_ID`` (default ``beamreach/slack-app``).
    Cached for a few minutes (a miss for one minute).
    """
    now = time.monotonic()
    ttl = _CACHE_HIT_TTL if _app_cache["value"] else _CACHE_MISS_TTL
    if _app_cache["loaded"] and now - _app_cache["at"] < ttl:
        return _app_cache["value"]
    value = _read_slack_app()
    _app_cache.update(at=now, value=value, loaded=True)
    return value


def reset_slack_app_cache() -> None:
    _app_cache.update(at=0.0, value=None, loaded=False)


# ── HTTP (injectable; tests replace it) ────────────────────────────────────

def _http_form(url: str, form: Dict[str, str], *, token: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
    """POST a urlencoded form (bearer ``token`` if given), return the parsed JSON body ({} when not JSON)."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read() or b""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── State ──────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str, key: str) -> str:
    return _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest())


def make_state(workspace_id: str, user_id: Optional[str], key: str, *, now: Optional[float] = None) -> str:
    payload = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "nonce": secrets.token_urlsafe(12),
        "exp": int((now if now is not None else time.time()) + STATE_TTL_SECONDS),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body, key)}"


class StateError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_state(state: str, key: str, workspace_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """The state's payload; raises StateError(reason) when tampered, expired or for another workspace."""
    body, _, sig = (state or "").partition(".")
    if not body or not sig or not hmac.compare_digest(sig, _sign(body, key)):
        raise StateError("invalid_state")
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        raise StateError("invalid_state") from None
    if not isinstance(payload, dict):
        raise StateError("invalid_state")
    if int(payload.get("exp") or 0) < (now if now is not None else time.time()):
        raise StateError("expired_state")
    if str(payload.get("workspace_id") or "") != str(workspace_id or ""):
        raise StateError("workspace_mismatch")
    return payload


# ── Connection (workspace config ``slack``) ────────────────────────────────

def workspace_slack(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The stored connection, or None when Slack is not connected."""
    slack = config.get("slack") if isinstance(config, dict) else None
    if isinstance(slack, dict) and str(slack.get("bot_token") or "").startswith("xoxb-"):
        return slack
    return None


def _require_connection(request: Request) -> Dict[str, Any]:
    slack = workspace_slack(get_workspace_config(request))
    if not slack:
        raise HTTPException(status_code=409, detail="Slack is not connected. Connect it in Settings → Slack.")
    return slack


# ── Helpers ────────────────────────────────────────────────────────────────

def _redirect_uri(request: Request) -> str:
    configured = (os.getenv("SLACK_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}{CALLBACK_PATH}"


def _back(ok: bool, reason: str = "") -> RedirectResponse:
    if ok:
        return RedirectResponse(f"{RETURN_PATH}&slack=connected", status_code=302)
    safe = re.sub(r"[^a-z0-9_]", "", (reason or "unknown").lower())[:64] or "unknown"
    return RedirectResponse(f"{RETURN_PATH}&slack=error&reason={safe}", status_code=302)


def _slack_error(data: Dict[str, Any], what: str) -> HTTPException:
    reason = str(data.get("error") or "no response")
    if reason in _DEAD_TOKEN_ERRORS:
        return HTTPException(status_code=409, detail=f"The Slack connection no longer works ({reason}). Reconnect Slack in Settings.")
    return HTTPException(status_code=502, detail=f"Slack {what} failed: {reason}")


# ── Routes: connection ─────────────────────────────────────────────────────

@router.get(f"{BASE_PATH}/status")
def slack_status(request: Request) -> Dict[str, Any]:
    out: Dict[str, Any] = {"available": load_slack_app() is not None, "connected": False}
    slack = workspace_slack(get_workspace_config(request))
    if slack:
        out.update(
            connected=True,
            team_id=slack.get("team_id") or None,
            team_name=slack.get("team_name") or None,
            connected_at=slack.get("connected_at") or None,
        )
    return out


@router.get(f"{BASE_PATH}/install")
def slack_install(request: Request) -> RedirectResponse:
    # A GET, so the write guard does not cover it.
    ensure_not_read_only()
    app = load_slack_app()
    if not app:
        raise HTTPException(
            status_code=503,
            detail="The Slack app is not configured for this installation (set SLACK_CLIENT_ID/SLACK_CLIENT_SECRET or the beamreach/slack-app secret).",
        )
    actor = get_actor_ids(request)
    state = make_state(str(actor.get("workspace_id") or ""), actor.get("user_id"), app["client_secret"])
    query = urllib.parse.urlencode({
        "client_id": app["client_id"],
        "scope": SLACK_SCOPES,
        "redirect_uri": _redirect_uri(request),
        "state": state,
    })
    return RedirectResponse(f"{SLACK_AUTHORIZE_URL}?{query}", status_code=302)


@router.get(f"{BASE_PATH}/callback")
def slack_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    if error:
        return _back(False, error)  # e.g. access_denied when the user cancels
    try:
        ensure_not_read_only()
    except HTTPException:
        return _back(False, "read_only")
    app = load_slack_app()
    if not app:
        return _back(False, "not_configured")
    try:
        actor = get_actor_ids(request)
        payload = verify_state(state or "", app["client_secret"], str(actor.get("workspace_id") or ""))
    except StateError as exc:
        logger.info("Slack connect refused: %s", exc.reason)
        return _back(False, exc.reason)
    if not code:
        return _back(False, "missing_code")

    try:
        data = _http_form(SLACK_OAUTH_ACCESS_URL, {
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "code": code,
            "redirect_uri": _redirect_uri(request),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Slack oauth.v2.access unreachable: %s", type(exc).__name__)
        return _back(False, "slack_unreachable")
    if not data.get("ok"):
        reason = str(data.get("error") or "oauth_failed")
        logger.info("Slack oauth.v2.access refused: %s", reason)
        return _back(False, reason)

    bot_token = str(data.get("access_token") or "")
    if not bot_token.startswith("xoxb-"):
        return _back(False, "no_bot_token")
    team = data.get("team") or {}
    connection = {
        "team_id": str(team.get("id") or ""),
        "team_name": str(team.get("name") or ""),
        "bot_token": bot_token,  # stored in the workspace secret, never returned
        "bot_user_id": str(data.get("bot_user_id") or ""),
        "scopes": str(data.get("scope") or ""),
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "connected_by": actor.get("email") or payload.get("user_id") or actor.get("user_id") or None,
    }
    try:
        config = get_workspace_config(request)
        config["slack"] = connection
        save_config(request, config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not store the Slack connection: %s", type(exc).__name__)
        return _back(False, "storage_failed")
    logger.info("Slack connected (team %s)", connection["team_id"])
    return _back(True)


@router.delete(BASE_PATH)
def slack_disconnect(request: Request) -> Dict[str, Any]:
    config = get_workspace_config(request)
    slack = config.get("slack")
    token = str((slack or {}).get("bot_token") or "") if isinstance(slack, dict) else ""
    revoked = False
    if token:
        try:
            revoked = bool(_http_form(SLACK_AUTH_REVOKE_URL, {}, token=token).get("revoked"))
        except Exception as exc:  # noqa: BLE001 - best effort; the connection is removed regardless
            logger.warning("Slack auth.revoke failed: %s", type(exc).__name__)
    if "slack" in config:
        config.pop("slack", None)
        save_config(request, config)
    return {"connected": False, "revoked": revoked}


@router.get(f"{BASE_PATH}/channels")
def slack_channels(request: Request) -> Dict[str, Any]:
    token = str(_require_connection(request)["bot_token"])
    channels: List[Dict[str, Any]] = []
    cursor = ""
    while len(channels) < CHANNELS_CAP:
        form = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"}
        if cursor:
            form["cursor"] = cursor
        try:
            data = _http_form(SLACK_CONVERSATIONS_LIST_URL, form, token=token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Slack unreachable: {type(exc).__name__}") from exc
        if not data.get("ok"):
            raise _slack_error(data, "conversations.list")
        for ch in data.get("channels") or []:
            if not isinstance(ch, dict) or not ch.get("id"):
                continue
            channels.append({
                "id": str(ch["id"]),
                "name": str(ch.get("name") or ch["id"]),
                "is_private": bool(ch.get("is_private")),
                "is_member": bool(ch.get("is_member")),
            })
        cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            break
    channels = channels[:CHANNELS_CAP]
    channels.sort(key=lambda c: (c["is_private"], c["name"].lower()))
    return {"channels": channels}


# ── Routes: alert channels that use the workspace connection ───────────────
# These belong with the other endpoint routes in notifications.py; they live
# here until that file can take the ``use_workspace_slack`` case.

class WorkspaceSlackEndpointPayload(BaseModel):
    channel: str = Field(..., min_length=1, description="Slack channel id")
    channel_name: Optional[str] = Field(default=None)
    is_private: bool = Field(default=False)
    name: Optional[str] = Field(default=None)


def _list_endpoint_items(table: Any, pk: str) -> List[Dict[str, Any]]:
    kce = Key("pk").eq(pk) & Key("sk").begins_with(notification_endpoint_prefix())
    resp = table.query(KeyConditionExpression=kce)
    items = list(resp.get("Items", []))
    while resp.get("LastEvaluatedKey"):
        resp = table.query(KeyConditionExpression=kce, ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


@router.post(f"{BASE_PATH}/endpoints")
def create_workspace_slack_endpoint(request: Request, body: WorkspaceSlackEndpointPayload) -> Dict[str, Any]:
    """A slack alert channel posting with the workspace's Slack connection."""
    slack = _require_connection(request)
    channel_id = body.channel.strip().lstrip("#")
    channel_name = (body.channel_name or "").strip().lstrip("#") or channel_id
    table = _notifications._resolve_table(request)
    pk = _notifications._ws_pk(request)
    for item in _list_endpoint_items(table, pk):
        if item.get("type") == "slack" and item.get("use_workspace_slack") and str(item.get("channel") or "") == channel_id:
            return _notifications._serialize_endpoint(item)
    endpoint_id = str(uuid.uuid4())
    item = {
        "pk": pk,
        "sk": notification_endpoint_sort_key(endpoint_id),
        "endpoint_id": endpoint_id,
        "name": (body.name or "").strip() or f"Slack #{channel_name}",
        "type": "slack",
        "use_workspace_slack": True,
        "channel": channel_id,
        "channel_name": channel_name,
        "is_private": bool(body.is_private),
        "team_id": str(slack.get("team_id") or ""),
        "created_at": _notifications._epoch_ms(),
    }
    try:
        table.put_item(Item=item)
    except Exception as exc:
        logger.exception("Failed to create the Slack alert channel")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _notifications._serialize_endpoint(item)


@router.post(f"{BASE_PATH}/endpoints/{{endpoint_id}}/test")
def test_workspace_slack_endpoint(request: Request, endpoint_id: str) -> Dict[str, Any]:
    from captains.honeypot import alert_dispatcher

    table = _notifications._resolve_table(request)
    pk = _notifications._ws_pk(request)
    item = table.get_item(Key={"pk": pk, "sk": notification_endpoint_sort_key(endpoint_id)}).get("Item")
    if not item or item.get("type") != "slack" or not item.get("use_workspace_slack"):
        raise HTTPException(status_code=404, detail="Slack alert channel not found")
    try:
        endpoint = alert_dispatcher.resolve_endpoint(item, get_workspace_config(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        alert_dispatcher._slack_bot_post(endpoint, text=TEST_MESSAGE, blocks=None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "sent", "endpoint_id": endpoint_id, "type": "slack"}
