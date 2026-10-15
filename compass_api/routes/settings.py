"""Workspace settings for the open features: AWS access, GitHub, and the bootstrap the dashboard loads.

The product adds its own sections (usage, billing, triage) and checks
(licence, payment link) through compass_api.extensions; its routes for
those are the product's and register through ``compass_api.extensions``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from compass_api import extensions
from compass_api.infra_scan import mark_infra_scan_pending
from compass_api.routes._app_config import ensure_settings_enabled, get_app_config
from compass_api.routes._infra_scan import current_infra_scan_summary, enqueue_full_infra_scan
from compass_api.routes._workspace import get_workspace_config, get_workspace_context, save_config
from compass_core import captain_admin
from compass_core import captains as _captains
from compass_core.aws import build_target_session, resolve_target_account_id, resolve_target_region
from compass_core.github import (
    build_github_app_manifest,
    exchange_manifest_code,
    get_installation_metadata,
    list_installation_repositories,
    load_github_app_env,
    load_github_app_from_config,
)
from compass_core.queue import queue_proxy

router = APIRouter()
queue_client = queue_proxy
logger = logging.getLogger(__name__)


GITHUB_INSTALL_COOKIE = "compass_github_install"
# Account Compass itself runs in: the trust principal for target-account roles.
# Set by the stack (COMPASS_ACCOUNT_ID); otherwise resolved once via STS; empty
# when neither is available (the role template then shows no principal).
_compass_account_id_cache: Optional[str] = None


def compass_account_id() -> str:
    global _compass_account_id_cache
    configured = (os.getenv("COMPASS_ACCOUNT_ID") or "").strip()
    if configured:
        return configured
    if _compass_account_id_cache:
        return _compass_account_id_cache
    try:
        import boto3

        _compass_account_id_cache = str(boto3.client("sts").get_caller_identity()["Account"])
    except Exception:
        _compass_account_id_cache = ""
    return _compass_account_id_cache


DEFAULT_ASSUME_ROLE_NAME = (os.getenv("TARGET_ROLE_NAME") or "BeamreachCompassRole").strip() or "BeamreachCompassRole"


def _strip_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _public_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    if not host:
        raise HTTPException(status_code=500, detail="Unable to resolve public host")
    return f"{proto}://{host}"


def _aws_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    aws_cfg = config.get("aws") or {}
    if not isinstance(aws_cfg, dict):
        aws_cfg = {}

    assume_cfg = aws_cfg.get("assume_role") if isinstance(aws_cfg.get("assume_role"), dict) else None
    credentials_cfg = aws_cfg.get("credentials") if isinstance(aws_cfg.get("credentials"), dict) else None
    stored_auth_mode = _strip_or_none(aws_cfg.get("auth_mode"))
    mode: Optional[str] = None
    if stored_auth_mode == "local_iam":
        mode = "local_iam"
    elif assume_cfg:
        mode = "assume_role"
    elif credentials_cfg:
        mode = "access_key"

    access_key_id = None
    if credentials_cfg:
        access_key_id = _strip_or_none(
            credentials_cfg.get("access_key_id") or credentials_cfg.get("aws_access_key_id")
        )

    return {
        "configured": mode is not None and (mode == "local_iam" or bool(assume_cfg or credentials_cfg)),
        "mode": mode,
        "region": _strip_or_none(aws_cfg.get("region") or config.get("aws_region")),
        "account_id": _strip_or_none(aws_cfg.get("account_id") or resolve_target_account_id(config)),
        "account_name": _strip_or_none(aws_cfg.get("account_name")),
        "role_name": _strip_or_none((assume_cfg or {}).get("role_name")),
        "external_id": _strip_or_none((assume_cfg or {}).get("external_id")) or _strip_or_none(aws_cfg.get("generated_external_id")),
        "external_id_configured": bool(_strip_or_none((assume_cfg or {}).get("external_id"))),
        "access_key_id_hint": f"...{access_key_id[-4:]}" if access_key_id and len(access_key_id) >= 4 else None,
        "last_validated_at": _strip_or_none(aws_cfg.get("last_validated_at")),
        "beamreach_account_id": compass_account_id(),
        "compass_account_id": compass_account_id(),
        "worker_role_name": (os.getenv("COMPASS_WORKER_ROLE_NAME") or f"compass-worker-task-{os.getenv('COMPASS_ENV') or os.getenv('BEAMREACH_ENV') or 'local'}"),
        "suggested_role_name": _strip_or_none((assume_cfg or {}).get("role_name")) or DEFAULT_ASSUME_ROLE_NAME,
    }


def _resolve_local_account(config: Dict[str, Any]) -> bool:
    """In local_iam mode the account is whichever the ambient credentials belong to: look it
    up once with STS and keep it in the config. Returns True when the config changed."""
    aws_cfg = config.get("aws") if isinstance(config.get("aws"), dict) else None
    if not aws_cfg or _strip_or_none(aws_cfg.get("auth_mode")) != "local_iam" or _strip_or_none(aws_cfg.get("account_id")):
        return False
    try:
        session = build_target_session(config, capability="read", region=resolve_target_region(config) or "us-east-1")
        account_id = session.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 - no credentials yet is a normal state on a fresh install
        logger.debug("cannot resolve the local account: %s", exc)
        return False
    aws_cfg["account_id"] = str(account_id)
    aws_cfg.setdefault("account_name", str(account_id))
    return True


def _github_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    github_cfg = config.get("github") or {}
    if not isinstance(github_cfg, dict):
        github_cfg = {}
    # Prefer workspace-specific App (manifest flow) over the shared global env.
    active_app = load_github_app_from_config(github_cfg) or load_github_app_env()

    return {
        "connected": bool(github_cfg.get("installation_id") or _strip_or_none(github_cfg.get("token"))),
        "mode": "github_app" if github_cfg.get("installation_id") else ("oauth_token" if _strip_or_none(github_cfg.get("token")) else None),
        "login": _strip_or_none(github_cfg.get("account_login") or github_cfg.get("login")),
        "account_type": _strip_or_none(github_cfg.get("account_type")),
        "installation_id": github_cfg.get("installation_id"),
        "repository_selection": _strip_or_none(github_cfg.get("repository_selection")),
        "repositories_count": github_cfg.get("repositories_count"),
        "connected_at": _strip_or_none(github_cfg.get("connected_at")),
        "app_configured": True,  # manifest flow: any customer can create their own App on demand
        "app_slug": active_app.slug if active_app else None,
        "public_link": active_app.public_link if active_app else None,
    }


def _ensure_generated_external_id(config: Dict[str, Any]) -> str:
    aws_cfg = config.setdefault("aws", {})
    if not isinstance(aws_cfg, dict):
        aws_cfg = {}
        config["aws"] = aws_cfg

    assume_cfg = aws_cfg.get("assume_role")
    if isinstance(assume_cfg, dict):
        existing_assume_external_id = _strip_or_none(assume_cfg.get("external_id"))
        if existing_assume_external_id:
            aws_cfg["generated_external_id"] = existing_assume_external_id
            return existing_assume_external_id

    generated_external_id = _strip_or_none(aws_cfg.get("generated_external_id"))
    if generated_external_id:
        return generated_external_id

    generated_external_id = f"beamreach-compass-{secrets.token_urlsafe(18)}"
    aws_cfg["generated_external_id"] = generated_external_id
    return generated_external_id


def _aws_validation_error_detail(payload: "AwsSettingsPayload", exc: Exception, *, stage: str) -> str:
    if payload.mode == "assume_role" and payload.assume_role is not None:
        role_arn = f"arn:aws:iam::{payload.assume_role.account_id}:role/{payload.assume_role.role_name}"
        if isinstance(exc, ClientError):
            error = exc.response.get("Error") or {}
            code = _strip_or_none(error.get("Code"))
            message = _strip_or_none(error.get("Message")) or str(exc)
            detail = f"Failed to assume AWS role {role_arn}: "
            if code:
                detail += f"{code}: "
            detail += message
            if code == "AccessDenied":
                detail += (
                    ". Verify the current AWS credentials allow sts:AssumeRole and the role trust policy permits this principal."
                )
            return detail
        return f"Failed to assume AWS role {role_arn} during {stage}: {exc}"

    return f"Failed to validate AWS credentials: {exc}"


def _validate_aws_access(payload: "AwsSettingsPayload") -> Dict[str, Any]:
    aws_config: Dict[str, Any] = {}
    if payload.region:
        aws_config["region"] = payload.region

    if payload.mode == "assume_role":
        assume_role = payload.assume_role
        if assume_role is None:
            raise HTTPException(status_code=400, detail="assume_role details are required")
        aws_config["assume_role"] = {
            "account_id": assume_role.account_id,
            "role_name": assume_role.role_name,
            **({"external_id": assume_role.external_id} if assume_role.external_id else {}),
        }
    elif payload.mode == "access_key":
        credentials = payload.access_key
        if credentials is None:
            raise HTTPException(status_code=400, detail="access_key credentials are required")
        aws_config["credentials"] = {
            "access_key_id": credentials.access_key_id,
            "secret_access_key": credentials.secret_access_key,
            **({"session_token": credentials.session_token} if credentials.session_token else {}),
        }
    # local_iam: use the task role — no extra aws_config needed

    try:
        session = build_target_session(
            {"aws": aws_config, "aws_region": payload.region},
            capability="read",
            allow_default=payload.mode == "local_iam",
        )
    except (BotoCoreError, ClientError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=_aws_validation_error_detail(payload, exc, stage="session creation")) from exc

    if session is None:
        raise HTTPException(status_code=400, detail="Unable to create target AWS session")

    try:
        sts = session.client("sts")
        identity = sts.get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=400, detail=_aws_validation_error_detail(payload, exc, stage="identity lookup")) from exc

    account_id = str(identity.get("Account") or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="AWS validation did not return an account id")

    return {
        "account_id": account_id,
        "region": payload.region or resolve_target_region({"aws": aws_config}),
        "caller_arn": str(identity.get("Arn") or ""),
    }


class AwsAssumeRolePayload(BaseModel):
    account_id: str = Field(..., min_length=12, max_length=12)
    role_name: str = Field(..., min_length=1)
    external_id: Optional[str] = None


class AwsAccessKeyPayload(BaseModel):
    access_key_id: str = Field(..., min_length=16)
    secret_access_key: str = Field(..., min_length=20)
    session_token: Optional[str] = None


class AwsSettingsPayload(BaseModel):
    mode: Literal["assume_role", "access_key", "local_iam"]
    region: Optional[str] = Field(default=None)
    account_name: Optional[str] = Field(default=None)
    assume_role: Optional[AwsAssumeRolePayload] = None
    access_key: Optional[AwsAccessKeyPayload] = None


# `triage`, `billing` and `infra_scan` each make their own round trip to
# DynamoDB — a findings query and two job listings — which together accounted
# for ~3.2s of a ~3.3s /api/settings/bootstrap. The sidebar polls this endpoint
# every 15s and the dashboard calls it on mount, so those reads were repeating
# constantly for data that barely moves. Resolve them concurrently and hold the
# result for a few seconds.
_BOOTSTRAP_SECTIONS_TTL_SECONDS = float(os.getenv("BOOTSTRAP_SECTIONS_TTL_SECONDS", "10"))
_bootstrap_sections_cache: Dict[str, Any] = {}
_bootstrap_sections_lock = threading.Lock()


def _bootstrap_sections(config: Dict[str, Any], request: Request, workspace_id: Optional[str]) -> Dict[str, Any]:
    cache_key = str(workspace_id or "")
    now = time.monotonic()

    if _BOOTSTRAP_SECTIONS_TTL_SECONDS > 0:
        with _bootstrap_sections_lock:
            cached = _bootstrap_sections_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    providers = {name: (lambda fn=fn: fn(config, request)) for name, fn in extensions.bootstrap_sections().items()}
    providers["infra_scan"] = lambda: current_infra_scan_summary(request, config=config)
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        pending = {name: pool.submit(fn) for name, fn in providers.items()}
        sections = {name: future.result() for name, future in pending.items()}

    if _BOOTSTRAP_SECTIONS_TTL_SECONDS > 0:
        with _bootstrap_sections_lock:
            _bootstrap_sections_cache[cache_key] = (now + _BOOTSTRAP_SECTIONS_TTL_SECONDS, sections)
    return sections


def invalidate_bootstrap_sections(workspace_id: Optional[str] = None) -> None:
    """Drop cached bootstrap sections so the next call re-reads DynamoDB.

    Called after actions that change usage counts or scan state, so the UI does
    not show a stale count for up to the TTL right after the user acts.
    """
    with _bootstrap_sections_lock:
        if workspace_id is None:
            _bootstrap_sections_cache.clear()
        else:
            _bootstrap_sections_cache.pop(str(workspace_id), None)


@router.get("/api/settings/bootstrap")
def get_settings_bootstrap(request: Request) -> Dict[str, Any]:
    from compass_api.routes.auth import _auth_enabled
    ctx = get_workspace_context(request)
    config = get_workspace_config(request)
    app = get_app_config(config)
    before_external_id = _strip_or_none(((config.get("aws") or {}).get("generated_external_id")))
    if app["settings_enabled"]:
        generated_external_id = _ensure_generated_external_id(config)
    else:
        generated_external_id = before_external_id
    if app["settings_enabled"] and generated_external_id != before_external_id:
        save_config(request, config)
    if app["settings_enabled"] and _resolve_local_account(config):
        save_config(request, config)
    extensions.run_bootstrap_hooks(config, ctx, request)
    sections = _bootstrap_sections(config, request, ctx.workspace_id)
    return {
        "workspace": {
            "workspace_id": ctx.workspace_id,
            "org_id": ctx.org_id,
            "user_id": ctx.user_id,
        },
        "app": {**app, "auth_enabled": _auth_enabled()},
        "core_version": _captains.core_version(),
        "captains": captain_admin.summary(config),
        "aws": _aws_summary(config),
        "github": _github_summary(config),
        # triage and billing come from the product; the open edition reports None
        "triage": sections.get("triage"),
        "billing": sections.get("billing"),
        "infra_scan": sections["infra_scan"],
        **{name: value for name, value in sections.items() if name not in ("triage", "billing", "infra_scan")},
    }


@router.post("/api/settings/aws/test")
def test_aws_settings(payload: AwsSettingsPayload) -> Dict[str, Any]:
    ensure_settings_enabled()
    validation = _validate_aws_access(payload)
    return {
        "ok": True,
        "aws": validation,
    }


@router.post("/api/settings/aws")
def update_aws_settings(payload: AwsSettingsPayload, request: Request) -> Dict[str, Any]:
    ensure_settings_enabled()
    config = get_workspace_config(request)
    validation = _validate_aws_access(payload)

    aws_cfg = config.setdefault("aws", {})
    if not isinstance(aws_cfg, dict):
        aws_cfg = {}
        config["aws"] = aws_cfg

    region = _strip_or_none(payload.region)
    if region:
        aws_cfg["region"] = region
        config["aws_region"] = region
    else:
        aws_cfg.pop("region", None)
        config.pop("aws_region", None)

    account_name = _strip_or_none(payload.account_name)
    aws_cfg["account_id"] = validation["account_id"]
    if account_name:
        aws_cfg["account_name"] = account_name
    else:
        aws_cfg.pop("account_name", None)

    aws_cfg["last_validated_at"] = datetime.now(timezone.utc).isoformat()
    aws_cfg["last_validated_caller_arn"] = validation["caller_arn"]

    if payload.mode == "assume_role":
        assume_role = payload.assume_role
        assert assume_role is not None
        generated_external_id = _ensure_generated_external_id(config)
        aws_cfg["assume_role"] = {
            "account_id": assume_role.account_id,
            "role_name": assume_role.role_name,
        }
        external_id = _strip_or_none(assume_role.external_id) or generated_external_id
        if external_id:
            aws_cfg["assume_role"]["external_id"] = external_id
        aws_cfg.pop("credentials", None)
    elif payload.mode == "access_key":
        access_key = payload.access_key
        assert access_key is not None
        aws_cfg["credentials"] = {
            "access_key_id": access_key.access_key_id,
            "secret_access_key": access_key.secret_access_key,
        }
        session_token = _strip_or_none(access_key.session_token)
        if session_token:
            aws_cfg["credentials"]["session_token"] = session_token
        aws_cfg.pop("assume_role", None)
        aws_cfg.pop("assume_role_read", None)
        aws_cfg.pop("assume_role_write", None)
    else:
        # local_iam: task role — clear any stored credentials
        aws_cfg.pop("credentials", None)
        aws_cfg.pop("assume_role", None)
        aws_cfg.pop("assume_role_read", None)
        aws_cfg.pop("assume_role_write", None)

    aws_cfg["auth_mode"] = payload.mode
    config["provider"] = "aws"
    mark_infra_scan_pending(config, reason="aws_settings_updated")

    save_config(request, config)
    jobs = extensions.run_aws_saved_hooks(request, config)
    mapper_job = enqueue_full_infra_scan(request, config=config, trigger="aws_settings_saved")
    return {
        "aws": _aws_summary(config),
        "audit": jobs,
        "infra_scan": current_infra_scan_summary(request, config=config),
        "mapper": mapper_job,
    }


class GithubTokenPayload(BaseModel):
    token: str = Field(..., min_length=20, description="GitHub token with repo scope (fine-grained or classic); stored, never returned")
    repositories: Optional[List[str]] = Field(default=None, description="Optional owner/name list to scan with Gitleaks/Checkov")


@router.post("/api/integrations/github/token")
def set_github_token(payload: GithubTokenPayload, request: Request) -> Dict[str, Any]:
    """Connect GitHub with a token instead of the App install flow (agents, CI, no browser)."""
    ensure_settings_enabled()
    token = payload.token.strip()
    try:
        import requests as _requests

        who = _requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub to validate the token: {exc}") from exc
    if who.status_code != 200:
        raise HTTPException(status_code=400, detail=f"GitHub rejected the token (HTTP {who.status_code})")
    login = str((who.json() or {}).get("login") or "").strip()
    config = get_workspace_config(request)
    github_cfg = config.get("github") if isinstance(config.get("github"), dict) else {}
    for stale in ("installation_id", "installation_account_login", "installation_account_type", "repository_selection", "repositories_count"):
        github_cfg.pop(stale, None)
    github_cfg["token"] = token
    github_cfg["account_login"] = login
    github_cfg["account_type"] = "token"
    github_cfg["connected_at"] = datetime.now(timezone.utc).isoformat()
    if payload.repositories:
        github_cfg["repositories"] = [r.strip() for r in payload.repositories if r and r.strip()]
    config["github"] = github_cfg
    save_config(request, config)
    invalidate_bootstrap_sections(get_workspace_context(request).workspace_id)
    scan_job = None
    try:
        scan_job = enqueue_full_infra_scan(request, trigger="github_token")
    except Exception:  # noqa: BLE001 - connecting must not fail because the scan could not start
        scan_job = None
    return {
        "connected": True,
        "mode": "oauth_token",
        "login": login,
        "repositories": github_cfg.get("repositories"),
        "infra_scan_job_id": (scan_job or {}).get("job_id") if isinstance(scan_job, dict) else None,
    }


@router.get("/api/integrations/github/connect")
def connect_github(request: Request):
    ensure_settings_enabled()

    ctx = get_workspace_context(request)
    nonce = secrets.token_urlsafe(32)
    cookie_payload = {
        "nonce": nonce,
        "workspace_id": ctx.workspace_id,
        "user_id": ctx.user_id,
        "issued_at": int(datetime.now(timezone.utc).timestamp()),
    }

    env = load_github_app_env()
    if env is not None:
        # Shared App configured via env vars — redirect straight to install page.
        redirect_url = env.install_url
    else:
        # Manifest flow: create a customer-specific GitHub App on the fly.
        # GitHub only accepts manifest data via POST — serve an auto-submitting form.
        config = get_workspace_config(request)
        aws_account_id = _strip_or_none((config.get("aws") or {}).get("account_id"))
        app_name = f"Beamreach Compass {aws_account_id}" if aws_account_id else "Beamreach Compass"
        origin = _public_origin(request)
        manifest = build_github_app_manifest(
            name=app_name,
            redirect_url=f"{origin}/api/integrations/github/manifest-exchange",
            setup_url=f"{origin}/api/integrations/github/setup",
        )
        manifest_json = json.dumps(manifest)
        cookie_value = json.dumps(cookie_payload, separators=(",", ":"))
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
            f"<form id=\"f\" method=\"post\" action=\"https://github.com/settings/apps/new?state={nonce}\">"
            "<input id=\"manifest\" type=\"hidden\" name=\"manifest\">"
            "</form>"
            "<script>"
            f"document.getElementById('manifest').value=JSON.stringify({manifest_json});"
            "document.getElementById('f').submit();"
            "</script>"
            "</body></html>"
        )
        from fastapi.responses import HTMLResponse
        response = HTMLResponse(content=html)
        response.set_cookie(
            GITHUB_INSTALL_COOKIE,
            cookie_value,
            max_age=600,
            httponly=True,
            secure=origin.startswith("https://"),
            samesite="lax",
            path="/api/integrations/github",
        )
        return response

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        GITHUB_INSTALL_COOKIE,
        json.dumps(cookie_payload, separators=(",", ":")),
        max_age=600,
        httponly=True,
        secure=_public_origin(request).startswith("https://"),
        samesite="lax",
        path="/api/integrations/github",
    )
    return response


@router.get("/api/integrations/github/manifest-exchange")
def github_manifest_exchange(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
):
    ensure_settings_enabled()
    ctx = get_workspace_context(request)
    raw_cookie = request.cookies.get(GITHUB_INSTALL_COOKIE)
    if not raw_cookie:
        raise HTTPException(status_code=400, detail="GitHub installation session is missing")
    try:
        cookie_payload = json.loads(raw_cookie)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="GitHub installation session is invalid") from exc

    if cookie_payload.get("workspace_id") != ctx.workspace_id or cookie_payload.get("user_id") != ctx.user_id:
        raise HTTPException(status_code=400, detail="GitHub installation session does not match the active workspace")
    if state != cookie_payload.get("nonce"):
        raise HTTPException(status_code=400, detail="GitHub installation state mismatch")
    if not code:
        raise HTTPException(status_code=400, detail="Missing GitHub manifest code")

    try:
        app_data = exchange_manifest_code(code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to exchange GitHub manifest code: {exc}") from exc

    config = get_workspace_config(request)
    github_cfg = config.setdefault("github", {})
    if not isinstance(github_cfg, dict):
        github_cfg = {}
        config["github"] = github_cfg

    github_cfg["app_id"] = str(app_data.get("id") or "")
    github_cfg["app_slug"] = _strip_or_none(app_data.get("slug"))
    github_cfg["client_id"] = _strip_or_none(app_data.get("client_id"))
    github_cfg["private_key"] = _strip_or_none(app_data.get("pem"))
    github_cfg["webhook_secret"] = _strip_or_none(app_data.get("webhook_secret"))
    github_cfg["app_html_url"] = _strip_or_none(app_data.get("html_url"))
    github_cfg["app_exchange_at"] = datetime.now(timezone.utc).isoformat()
    # Clear any stale installation from a previous connection.
    for key in ("installation_id", "installation_target_id", "repository_selection",
                "account_login", "account_type", "account_id", "repositories_count", "connected_at"):
        github_cfg.pop(key, None)

    save_config(request, config)

    app_slug = github_cfg["app_slug"]
    response = RedirectResponse(
        url=f"https://github.com/apps/{app_slug}/installations/new",
        status_code=302,
    )
    return response


@router.get("/api/integrations/github/setup")
def github_setup(
    request: Request,
    installation_id: Optional[int] = None,
    setup_action: Optional[str] = None,
):
    ensure_settings_enabled()
    ctx = get_workspace_context(request)
    raw_cookie = request.cookies.get(GITHUB_INSTALL_COOKIE)
    if not raw_cookie:
        raise HTTPException(status_code=400, detail="GitHub installation session is missing")
    try:
        cookie_payload = json.loads(raw_cookie)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="GitHub installation session is invalid") from exc

    if cookie_payload.get("workspace_id") != ctx.workspace_id or cookie_payload.get("user_id") != ctx.user_id:
        raise HTTPException(status_code=400, detail="GitHub installation session does not match the active workspace")
    if installation_id is None:
        raise HTTPException(status_code=400, detail="Missing GitHub installation id")

    config = get_workspace_config(request)
    workspace_app_env = load_github_app_from_config(config.get("github") or {})

    try:
        installation = get_installation_metadata(int(installation_id), app_env=workspace_app_env)
        repositories = list_installation_repositories(int(installation_id), app_env=workspace_app_env)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to inspect GitHub installation: {exc}") from exc

    account = installation.get("account") or {}
    env = load_github_app_env()
    github_cfg = config.setdefault("github", {})
    if not isinstance(github_cfg, dict):
        github_cfg = {}
        config["github"] = github_cfg

    github_cfg["provider"] = "github_app"
    github_cfg["installation_id"] = int(installation_id)
    github_cfg["installation_target_id"] = installation.get("target_id")
    github_cfg["repository_selection"] = _strip_or_none(installation.get("repository_selection"))
    github_cfg["account_login"] = _strip_or_none(account.get("login"))
    github_cfg["account_type"] = _strip_or_none(account.get("type"))
    github_cfg["account_id"] = account.get("id")
    github_cfg["repositories_count"] = repositories.get("total_count")
    if env is not None:
        github_cfg["app_slug"] = env.slug
    github_cfg["setup_action"] = _strip_or_none(setup_action)
    github_cfg["connected_at"] = datetime.now(timezone.utc).isoformat()
    github_cfg.pop("token", None)
    github_cfg.pop("refresh_token", None)
    github_cfg.pop("scope", None)
    github_cfg.pop("token_type", None)
    github_cfg.pop("token_expires_at", None)
    github_cfg.pop("refresh_token_expires_at", None)
    github_cfg.pop("login", None)
    github_cfg.pop("user_id", None)
    mark_infra_scan_pending(config, reason="github_integration_updated")

    save_config(request, config)
    enqueue_full_infra_scan(request, config=config, trigger="github_connected")

    response = RedirectResponse(url="/workspace?tab=settings&github=connected", status_code=302)
    response.delete_cookie(GITHUB_INSTALL_COOKIE, path="/api/integrations/github")
    return response


@router.get("/api/integrations/github/callback")
def github_callback_alias(
    request: Request,
    installation_id: Optional[int] = None,
    setup_action: Optional[str] = None,
):
    ensure_settings_enabled()
    return github_setup(request, installation_id=installation_id, setup_action=setup_action)
