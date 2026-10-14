from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

_CACHE_LOCK = threading.Lock()
_SECRET_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _cache_ttl_seconds() -> int:
    raw = (os.getenv("RUNTIME_SECRET_CACHE_TTL_SECONDS") or "").strip()
    if not raw:
        return 300
    try:
        value = int(raw)
    except ValueError:
        return 300
    return max(30, value)


def _strip_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _secrets_client():
    region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
    kwargs = {"region_name": region} if region else {}
    return boto3.client("secretsmanager", **kwargs)


def _parse_secret_payload(secret_id: str, response: Dict[str, Any]) -> Dict[str, Any]:
    secret_string = response.get("SecretString")
    if secret_string:
        try:
            payload = json.loads(secret_string)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Secret '{secret_id}' does not contain valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Secret '{secret_id}' must contain a JSON object")
        return payload

    secret_binary = response.get("SecretBinary")
    if secret_binary:
        try:
            if isinstance(secret_binary, bytes):
                decoded = secret_binary.decode("utf-8")
            else:
                decoded = str(secret_binary)
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Secret '{secret_id}' binary payload is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Secret '{secret_id}' must contain a JSON object")
        return payload

    raise RuntimeError(f"Secret '{secret_id}' did not contain SecretString or SecretBinary")


def load_json_secret(secret_id: str) -> Dict[str, Any]:
    secret_id = (secret_id or "").strip()
    if not secret_id:
        raise RuntimeError("Secret id is required")

    now = time.time()
    with _CACHE_LOCK:
        cached = _SECRET_CACHE.get(secret_id)
        if cached and cached[0] > now:
            return dict(cached[1])

    client = _secrets_client()
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to read secret '{secret_id}': {exc}") from exc

    payload = _parse_secret_payload(secret_id, response)
    ttl = _cache_ttl_seconds()
    with _CACHE_LOCK:
        _SECRET_CACHE[secret_id] = (now + ttl, dict(payload))
    return payload


def load_json_secret_from_env(secret_arn_env_var: str) -> Optional[Dict[str, Any]]:
    secret_id = _strip_or_none(os.getenv(secret_arn_env_var))
    if not secret_id:
        return None
    return load_json_secret(secret_id)


def get_secret_field_from_env(secret_arn_env_var: str, field: str) -> Optional[str]:
    payload = load_json_secret_from_env(secret_arn_env_var)
    if not payload:
        return None
    return _strip_or_none(payload.get(field))
