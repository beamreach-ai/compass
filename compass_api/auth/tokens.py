"""API token store: personal tokens in DynamoDB, bootstrap token from Secrets Manager.

Token format: ``cmp_`` + 43 base64url chars (32 random bytes). Only the SHA-256
hash is stored. Two items per token in the workspaces table:

* lookup item   pk=TOKEN#<sha256>      sk=TOKEN               (resolve a bearer)
* listing item  pk=WORKSPACE#<ws>      sk=TOKEN#<token_id>    (list / revoke)

Without ``WORKSPACES_TABLE`` (local dev, unit tests) the store is in-memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from compass_api.auth.principal import ApiPrincipal, normalize_scope
from compass_core import sessions

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "cmp_"
_LOOKUP_SK = "TOKEN"
_LISTING_SK_PREFIX = "TOKEN#"
_RESOLVE_CACHE_TTL = int(os.getenv("API_TOKEN_CACHE_TTL", "60"))
_LAST_USED_WRITE_INTERVAL = 300
_BOOTSTRAP_CACHE_TTL = int(os.getenv("BOOTSTRAP_TOKEN_CACHE_TTL", "300"))


class TokenStoreError(RuntimeError):
    pass


def generate_token() -> str:
    raw = secrets.token_bytes(32)
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(value: Optional[str]) -> bool:
    return bool(value) and str(value).startswith(TOKEN_PREFIX)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_token_id() -> str:
    return "tok_" + secrets.token_hex(8)


# --------------------------------------------------------------------------- storage


class _MemoryStore:
    def __init__(self) -> None:
        self._by_hash: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._by_hash[record["token_hash"]] = dict(record)

    def get_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._by_hash.get(token_hash)
            return dict(record) if record else None

    def list_for_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._by_hash.values() if r.get("workspace_id") == workspace_id]

    def find(self, workspace_id: str, token_id: str) -> Optional[Dict[str, Any]]:
        for record in self.list_for_workspace(workspace_id):
            if record.get("token_id") == token_id:
                return record
        return None

    def update(self, token_hash: str, **fields: Any) -> None:
        with self._lock:
            record = self._by_hash.get(token_hash)
            if record:
                record.update(fields)

    def clear(self) -> None:
        with self._lock:
            self._by_hash.clear()


class _DynamoStore:
    def __init__(self, table_name: str) -> None:
        region = os.getenv("WORKSPACES_TABLE_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        self._table = sessions.control(region=region).resource("dynamodb").Table(table_name)
        self._hash_key = os.getenv("WORKSPACES_TABLE_HASH_KEY", "pk")
        self._range_key = os.getenv("WORKSPACES_TABLE_RANGE_KEY", "sk")

    def _lookup_key(self, token_hash: str) -> Dict[str, str]:
        return {self._hash_key: f"TOKEN#{token_hash}", self._range_key: _LOOKUP_SK}

    def _listing_key(self, workspace_id: str, token_id: str) -> Dict[str, str]:
        return {self._hash_key: f"WORKSPACE#{workspace_id}", self._range_key: f"{_LISTING_SK_PREFIX}{token_id}"}

    @staticmethod
    def _strip_keys(item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: (int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, float) and k.endswith("_at") else v)
                for k, v in item.items() if k not in ("pk", "sk")}

    def put(self, record: Dict[str, Any]) -> None:
        try:
            self._table.put_item(Item={**self._lookup_key(record["token_hash"]), **record})
            self._table.put_item(Item={**self._listing_key(record["workspace_id"], record["token_id"]), **record})
        except (BotoCoreError, ClientError) as exc:
            raise TokenStoreError(f"Failed to store API token: {exc}") from exc

    def get_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table.get_item(Key=self._lookup_key(token_hash), ConsistentRead=True)
        except (BotoCoreError, ClientError) as exc:
            raise TokenStoreError(f"Failed to look up API token: {exc}") from exc
        item = response.get("Item")
        return self._strip_keys(item) if item else None

    def list_for_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        condition = Key(self._hash_key).eq(f"WORKSPACE#{workspace_id}") & Key(self._range_key).begins_with(_LISTING_SK_PREFIX)
        items: List[Dict[str, Any]] = []
        try:
            response = self._table.query(KeyConditionExpression=condition)
            items.extend(response.get("Items", []))
            while response.get("LastEvaluatedKey"):
                response = self._table.query(KeyConditionExpression=condition, ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get("Items", []))
        except (BotoCoreError, ClientError) as exc:
            raise TokenStoreError(f"Failed to list API tokens: {exc}") from exc
        return [self._strip_keys(item) for item in items]

    def find(self, workspace_id: str, token_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._table.get_item(Key=self._listing_key(workspace_id, token_id), ConsistentRead=True)
        except (BotoCoreError, ClientError) as exc:
            raise TokenStoreError(f"Failed to load API token: {exc}") from exc
        item = response.get("Item")
        return self._strip_keys(item) if item else None

    def update(self, token_hash: str, **fields: Any) -> None:
        record = self.get_by_hash(token_hash)
        if not record:
            return
        record.update(fields)
        self.put(record)


_store_lock = threading.Lock()
_store: Optional[Any] = None
_store_table: Optional[str] = None


def _get_store():
    global _store, _store_table
    table_name = (os.getenv("WORKSPACES_TABLE") or "").strip() or None
    with _store_lock:
        if _store is None or _store_table != table_name:
            _store = _DynamoStore(table_name) if table_name else _MemoryStore()
            _store_table = table_name
        return _store


def reset_store_for_tests() -> None:
    global _store, _store_table
    with _store_lock:
        _store = None
        _store_table = None
    with _cache_lock:
        _resolve_cache.clear()
    _bootstrap_cache.clear()


# --------------------------------------------------------------------------- public API


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "token_id": record.get("token_id"),
        "name": record.get("name"),
        "scope": record.get("scope"),
        "kind": record.get("kind", "personal"),
        "workspace_id": record.get("workspace_id"),
        "user_id": record.get("user_id"),
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
        "revoked_at": record.get("revoked_at"),
    }


def create_token(
    *,
    workspace_id: str,
    name: str,
    scope: str,
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a personal token. Returns the public record plus the plaintext ``token`` (shown once)."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    clean_name = str(name or "").strip()[:80] or "api token"
    plaintext = generate_token()
    record = {
        "token_id": _new_token_id(),
        "token_hash": hash_token(plaintext),
        "kind": "personal",
        "name": clean_name,
        "scope": normalize_scope(scope),
        "workspace_id": workspace_id,
        "org_id": org_id,
        "user_id": user_id,
        "created_at": _now_ms(),
        "last_used_at": None,
        "revoked_at": None,
    }
    _get_store().put(record)
    return {**_public_record(record), "token": plaintext}


def list_tokens(workspace_id: str) -> List[Dict[str, Any]]:
    records = _get_store().list_for_workspace(workspace_id)
    records.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return [_public_record(r) for r in records]


def revoke_token(workspace_id: str, token_id: str) -> Optional[Dict[str, Any]]:
    store = _get_store()
    record = store.find(workspace_id, token_id)
    if not record:
        return None
    if not record.get("revoked_at"):
        store.update(record["token_hash"], revoked_at=_now_ms())
        record["revoked_at"] = _now_ms()
    with _cache_lock:
        _resolve_cache.pop(record["token_hash"], None)
    return _public_record(record)


# --------------------------------------------------------------------------- resolution

_cache_lock = threading.Lock()
_resolve_cache: Dict[str, tuple[float, Optional[ApiPrincipal]]] = {}


def _principal_from_record(record: Dict[str, Any]) -> Optional[ApiPrincipal]:
    if record.get("revoked_at"):
        return None
    return ApiPrincipal(
        token_id=str(record.get("token_id") or ""),
        kind=str(record.get("kind") or "personal"),
        scope=normalize_scope(record.get("scope")),
        workspace_id=str(record.get("workspace_id") or ""),
        org_id=record.get("org_id") or None,
        user_id=record.get("user_id") or None,
        name=record.get("name") or None,
    )


def _touch_last_used(record: Dict[str, Any]) -> None:
    last = record.get("last_used_at") or 0
    now = _now_ms()
    if now - int(last or 0) < _LAST_USED_WRITE_INTERVAL * 1000:
        return
    try:
        _get_store().update(record["token_hash"], last_used_at=now)
    except Exception:  # best effort
        logger.debug("Could not update last_used_at for token %s", record.get("token_id"))


def resolve_token(token: Optional[str]) -> Optional[ApiPrincipal]:
    """Return the principal for a bearer token, or None when unknown or revoked."""
    if not looks_like_token(token):
        return None
    token = str(token).strip()

    bootstrap = _bootstrap_principal_if_matches(token)
    if bootstrap is not None:
        return bootstrap

    token_hash = hash_token(token)
    now = time.time()
    with _cache_lock:
        cached = _resolve_cache.get(token_hash)
        if cached and cached[0] > now:
            return cached[1]

    try:
        record = _get_store().get_by_hash(token_hash)
    except TokenStoreError as exc:
        logger.error("API token lookup failed: %s", exc)
        return None
    principal = _principal_from_record(record) if record else None
    with _cache_lock:
        _resolve_cache[token_hash] = (now + _RESOLVE_CACHE_TTL, principal)
    if principal is not None and record is not None:
        _touch_last_used(record)
    return principal


# --------------------------------------------------------------------------- bootstrap token

_bootstrap_cache: Dict[str, Any] = {}


def _load_bootstrap_secret() -> Optional[str]:
    """Read the bootstrap token from env or Secrets Manager (cached)."""
    env_value = (os.getenv("BOOTSTRAP_TOKEN") or "").strip()
    if env_value:
        return env_value
    arn = (os.getenv("BOOTSTRAP_TOKEN_SECRET_ARN") or "").strip()
    if not arn:
        return None
    now = time.time()
    cached = _bootstrap_cache.get(arn)
    if cached and cached[0] > now:
        return cached[1]
    value: Optional[str] = None
    try:
        region = os.getenv("WORKSPACE_SECRETS_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        client = sessions.control(region=region).client("secretsmanager")
        payload = client.get_secret_value(SecretId=arn).get("SecretString") or ""
        payload = payload.strip()
        if payload.startswith("{"):
            data = json.loads(payload)
            value = str(data.get("token") or data.get("bootstrap_token") or "").strip() or None
        else:
            value = payload or None
    except Exception as exc:
        logger.error("Could not read bootstrap token secret %s: %s", arn, exc)
    _bootstrap_cache[arn] = (now + _BOOTSTRAP_CACHE_TTL, value)
    return value


def bootstrap_workspace_id() -> str:
    return (os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET") or "default").strip() or "default"


def effective_bootstrap_token(secret_value: Optional[str]) -> Optional[str]:
    """The stack generates a bare random string; callers present it as ``cmp_<value>``."""
    value = (secret_value or "").strip()
    if not value:
        return None
    return value if value.startswith(TOKEN_PREFIX) else TOKEN_PREFIX + value


def _bootstrap_principal_if_matches(token: str) -> Optional[ApiPrincipal]:
    secret = effective_bootstrap_token(_load_bootstrap_secret())
    if not secret:
        return None
    if not hmac.compare_digest(secret.encode("utf-8"), token.encode("utf-8")):
        return None
    return ApiPrincipal(
        token_id="bootstrap",
        kind="bootstrap",
        scope="admin",
        workspace_id=bootstrap_workspace_id(),
        org_id=None,
        user_id="bootstrap",
        name="bootstrap",
    )


def bootstrap_configured() -> bool:
    return bool((os.getenv("BOOTSTRAP_TOKEN") or "").strip() or (os.getenv("BOOTSTRAP_TOKEN_SECRET_ARN") or "").strip())
