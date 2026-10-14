from __future__ import annotations

import base64
import calendar
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from compass_core.secrets import get_secret_field_from_env

GITHUB_API = "https://api.github.com"
_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE: Dict[Tuple[int, str], Tuple[float, Dict[str, Any]]] = {}


def _strip_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


@dataclass(frozen=True)
class GitHubAppEnv:
    app_id: str
    client_id: Optional[str]
    private_key: str
    slug: str

    @property
    def public_link(self) -> str:
        return f"https://github.com/apps/{self.slug}"

    @property
    def install_url(self) -> str:
        return f"{self.public_link}/installations/new"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_github_app_from_config(github_cfg: Dict[str, Any]) -> Optional[GitHubAppEnv]:
    """Load GitHub App credentials stored in the workspace config by the manifest flow."""
    if not isinstance(github_cfg, dict):
        return None
    app_id = _strip_or_none(github_cfg.get("app_id"))
    private_key = _strip_or_none(github_cfg.get("private_key"))
    slug = _strip_or_none(github_cfg.get("app_slug"))
    client_id = _strip_or_none(github_cfg.get("client_id"))
    if not app_id or not private_key or not slug:
        return None
    if "\\n" in private_key and "-----BEGIN" in private_key:
        private_key = private_key.replace("\\n", "\n")
    return GitHubAppEnv(app_id=app_id, client_id=client_id, private_key=private_key, slug=slug)


def build_github_app_manifest(name: str, redirect_url: str, setup_url: str) -> Dict[str, Any]:
    return {
        "name": name,
        "url": "https://github.com/beamreach-ai/compass",
        "hook_attributes": {"active": False},
        "redirect_url": redirect_url,
        "setup_url": setup_url,
        "setup_on_update": True,
        "callback_urls": [],
        "public": False,
        "default_permissions": {
            "contents": "write",
            "metadata": "read",
            "pull_requests": "write",
        },
        "default_events": [],
    }


def exchange_manifest_code(code: str) -> Dict[str, Any]:
    """Exchange a one-time GitHub manifest code for App credentials."""
    response = requests.post(
        f"{GITHUB_API}/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "beamreach-compass",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"GitHub manifest exchange failed ({response.status_code}): {detail}")
    return response.json()


def load_github_app_env() -> Optional[GitHubAppEnv]:
    app_id = _strip_or_none(os.getenv("GITHUB_APP_ID"))
    private_key = _strip_or_none(os.getenv("GITHUB_APP_PRIVATE_KEY"))
    slug = _strip_or_none(os.getenv("GITHUB_APP_SLUG"))
    client_id = _strip_or_none(os.getenv("GITHUB_APP_CLIENT_ID"))

    if not app_id or not private_key or not slug:
        try:
            if not app_id:
                app_id = get_secret_field_from_env("GITHUB_APP_SECRET_ARN", "app_id")
            if not slug:
                slug = get_secret_field_from_env("GITHUB_APP_SECRET_ARN", "app_slug")
            if not client_id:
                client_id = get_secret_field_from_env("GITHUB_APP_SECRET_ARN", "client_id")
            if not private_key:
                private_key = (
                    get_secret_field_from_env("GITHUB_APP_PRIVATE_KEY_SECRET_ARN", "private_key")
                    or get_secret_field_from_env("GITHUB_APP_SECRET_ARN", "private_key")
                )
        except RuntimeError:
            return None

    if not app_id or not private_key or not slug:
        return None
    if "\\n" in private_key and "-----BEGIN" in private_key:
        private_key = private_key.replace("\\n", "\n")
    return GitHubAppEnv(app_id=app_id, client_id=client_id, private_key=private_key, slug=slug)


def github_app_configured() -> bool:
    return load_github_app_env() is not None


def _build_app_jwt(env: GitHubAppEnv) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,
        "exp": now + 9 * 60,
        "iss": env.app_id,
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    private_key = serialization.load_pem_private_key(env.private_key.encode("utf-8"), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _github_request(method: str, path: str, *, bearer: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.request(
        method,
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "beamreach-compass",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=json_body,
        timeout=20,
    )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"GitHub API {method} {path} failed ({response.status_code}): {detail}")
    return response.json()


def _github_paginated_get(path: str, *, bearer: str, list_key: Optional[str] = None) -> List[Dict[str, Any]]:
    page = 1
    items: List[Dict[str, Any]] = []
    while True:
        separator = "&" if "?" in path else "?"
        response = requests.get(
            f"{GITHUB_API}{path}{separator}per_page=100&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer}",
                "User-Agent": "beamreach-compass",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"GitHub API GET {path} failed ({response.status_code}): {detail}")
        payload = response.json()
        if list_key:
            page_items = payload.get(list_key) if isinstance(payload, dict) else None
        else:
            page_items = payload
        if not isinstance(page_items, list):
            expected = list_key or "list"
            raise RuntimeError(f"GitHub API GET {path} did not return a '{expected}' payload")
        items.extend(entry for entry in page_items if isinstance(entry, dict))
        if len(page_items) < 100:
            break
        page += 1
    return items


def get_installation_metadata(installation_id: int, *, app_env: Optional[GitHubAppEnv] = None) -> Dict[str, Any]:
    env = app_env or load_github_app_env()
    if env is None:
        raise RuntimeError("GitHub App is not configured")
    jwt_token = _build_app_jwt(env)
    return _github_request("GET", f"/app/installations/{installation_id}", bearer=jwt_token)


def get_installation_token(
    installation_id: int,
    *,
    permissions: Optional[Dict[str, str]] = None,
    cache_scope: str = "default",
    app_env: Optional[GitHubAppEnv] = None,
) -> Dict[str, Any]:
    cache_key = (installation_id, cache_scope)
    now = time.time()
    with _CACHE_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    env = app_env or load_github_app_env()
    if env is None:
        raise RuntimeError("GitHub App is not configured")
    jwt_token = _build_app_jwt(env)
    body: Dict[str, Any] = {}
    if permissions:
        body["permissions"] = permissions
    token_payload = _github_request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        bearer=jwt_token,
        json_body=body or None,
    )

    expires_at = _strip_or_none(token_payload.get("expires_at"))
    ttl = 45 * 60
    if expires_at:
        try:
            ttl = max(60, int(calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")) - now - 300))
        except Exception:
            ttl = 45 * 60
    with _CACHE_LOCK:
        _TOKEN_CACHE[cache_key] = (now + ttl, dict(token_payload))
    return token_payload


def list_installation_repositories(installation_id: int, *, app_env: Optional[GitHubAppEnv] = None) -> Dict[str, Any]:
    token_payload = get_installation_token(installation_id, cache_scope="repo-list", app_env=app_env)
    token = _strip_or_none(token_payload.get("token"))
    if not token:
        raise RuntimeError("GitHub installation token is missing")
    repositories = _github_paginated_get("/installation/repositories", bearer=token, list_key="repositories")
    return {
        "total_count": len(repositories),
        "repositories": repositories,
    }


def resolve_github_token(config: Any, *, capability: str = "read") -> Optional[str]:
    if not isinstance(config, dict):
        return None
    github_cfg = config.get("github")
    if not isinstance(github_cfg, dict):
        return None

    if capability == "write":
        token = github_cfg.get("token_write") or github_cfg.get("write_token") or github_cfg.get("token")
    else:
        token = github_cfg.get("token_read") or github_cfg.get("read_token") or github_cfg.get("token")
    token = _strip_or_none(token)
    if token:
        return token

    installation_id_raw = github_cfg.get("installation_id")
    try:
        installation_id = int(installation_id_raw) if installation_id_raw is not None else None
    except (TypeError, ValueError):
        installation_id = None
    if installation_id is None:
        return None

    workspace_app_env = load_github_app_from_config(github_cfg)
    permissions = None
    if capability == "read":
        permissions = {"contents": "read", "metadata": "read"}
    token_payload = get_installation_token(
        installation_id,
        permissions=permissions,
        cache_scope=f"phase:{capability}",
        app_env=workspace_app_env,
    )
    return _strip_or_none(token_payload.get("token"))


def _repo_installation_id(owner: str, repo: str, *, app_env: Optional[GitHubAppEnv] = None) -> Optional[int]:
    env = app_env or load_github_app_env()
    if env is None:
        return None
    jwt_token = _build_app_jwt(env)
    payload = _github_request("GET", f"/repos/{owner}/{repo}/installation", bearer=jwt_token)
    installation_raw = payload.get("id") if isinstance(payload, dict) else None
    try:
        return int(installation_raw) if installation_raw is not None else None
    except (TypeError, ValueError):
        return None


def resolve_github_token_for_repo(config: Any, *, repo_slug: str, capability: str = "write") -> Optional[str]:
    owner = ""
    repo = ""
    if isinstance(repo_slug, str) and "/" in repo_slug:
        owner, repo = [part.strip() for part in repo_slug.split("/", 1)]
    if not owner or not repo:
        return resolve_github_token(config, capability=capability)

    github_cfg = config.get("github") if isinstance(config, dict) and isinstance(config.get("github"), dict) else {}
    workspace_app_env = load_github_app_from_config(github_cfg) if isinstance(github_cfg, dict) else None
    installation_id: Optional[int] = None
    installation_raw = github_cfg.get("installation_id") if isinstance(github_cfg, dict) else None
    try:
        installation_id = int(installation_raw) if installation_raw is not None else None
    except (TypeError, ValueError):
        installation_id = None

    if installation_id is None:
        try:
            installation_id = _repo_installation_id(owner, repo, app_env=workspace_app_env)
        except Exception:
            installation_id = None

    if installation_id is not None:
        permissions = {"contents": "read", "metadata": "read"} if capability == "read" else None
        try:
            token_payload = get_installation_token(
                installation_id,
                permissions=permissions,
                cache_scope=f"repo:{owner}/{repo}:phase:{capability}",
                app_env=workspace_app_env,
            )
            token = _strip_or_none(token_payload.get("token"))
            if token:
                return token
        except Exception:
            pass

    return resolve_github_token(config, capability=capability)


def list_configured_repositories(config: Any, *, capability: str = "read") -> Tuple[Optional[str], List[Dict[str, Any]]]:
    if not isinstance(config, dict):
        return None, []
    github_cfg = config.get("github")
    if not isinstance(github_cfg, dict):
        return None, []

    token = resolve_github_token(config, capability=capability)
    if not token:
        return None, []

    installation_id_raw = github_cfg.get("installation_id")
    try:
        installation_id = int(installation_id_raw) if installation_id_raw is not None else None
    except (TypeError, ValueError):
        installation_id = None

    if installation_id is not None:
        workspace_app_env = load_github_app_from_config(github_cfg)
        payload = list_installation_repositories(installation_id, app_env=workspace_app_env)
        repos = payload.get("repositories") if isinstance(payload, dict) else None
        return token, repos if isinstance(repos, list) else []

    orgs_cfg = github_cfg.get("orgs")
    orgs: List[str] = []
    if isinstance(orgs_cfg, list):
        orgs = [_strip_or_none(org) for org in orgs_cfg]
        orgs = [org for org in orgs if org]
    else:
        org_value = _strip_or_none(github_cfg.get("org"))
        if org_value:
            orgs = [org_value]

    repositories: List[Dict[str, Any]] = []
    for org in orgs:
        repositories.extend(
            _github_paginated_get(
                f"/orgs/{org}/repos",
                bearer=token,
                list_key=None,
            )
        )
    return token, repositories
