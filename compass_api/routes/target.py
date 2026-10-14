import os
from typing import Any, Dict, Iterable, Optional, Sequence

from fastapi import APIRouter, HTTPException, Request

from compass_api.routes._workspace import get_workspace_config

router = APIRouter()


def _normalize_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if isinstance(value, (int, float)):
        # Avoid scientific notation for large account identifiers
        if isinstance(value, float) and not value.is_integer():
            return None
        return str(int(value)) if isinstance(value, float) else str(value)
    return None


def _pluck(config: Dict[str, Any], path: Sequence[str]) -> Optional[str]:
    current: Any = config
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return _normalize_string(current)


def _search_nested(config: Any, keys: Iterable[str]) -> Optional[str]:
    target_keys = {key.lower() for key in keys}
    stack: list[Any] = [config]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)

        if isinstance(current, dict):
            for key, value in current.items():
                if key.lower() in target_keys:
                    normalized = _normalize_string(value)
                    if normalized:
                        return normalized
                if isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)

    return None


def _extract_account_id(config: Dict[str, Any]) -> Optional[str]:
    candidate_paths: Iterable[Sequence[str]] = [
        ("account_id",),
        ("account",),
        ("aws", "account_id"),
        ("aws", "account"),
        ("aws", "assume_role", "account_id"),
        ("aws", "assume_role_read", "account_id"),
        ("aws", "assume_role_write", "account_id"),
        ("assume_role", "account_id"),
        ("assume_role_read", "account_id"),
        ("assume_role_write", "account_id"),
        ("metadata", "account_id"),
    ]

    for path in candidate_paths:
        value = _pluck(config, path)
        if value:
            return value

    return _search_nested(config, ("account_id", "account", "aws_account_id", "AccountId"))


def _extract_account_name(config: Dict[str, Any], target: str) -> Dict[str, Optional[str]]:
    primary_candidates: Iterable[Sequence[str]] = [
        ("account_name",),
        ("name",),
        ("metadata", "account_name"),
        ("aws", "account_name"),
        ("aws", "name"),
    ]
    alias_candidates: Iterable[Sequence[str]] = [
        ("account_alias",),
        ("metadata", "account_alias"),
        ("aws", "account_alias"),
        ("target_name",),
        ("target",),
        ("env",),
        ("environment",),
    ]

    name = None
    for path in primary_candidates:
        value = _pluck(config, path)
        if value:
            name = value
            break

    alias = None
    for path in alias_candidates:
        value = _pluck(config, path)
        if value:
            alias = value
            break

    if not name:
        name = alias or target
    if not alias:
        alias = target

    return {"name": _normalize_string(name), "alias": _normalize_string(alias)}


def _extract_environment(config: Dict[str, Any]) -> Optional[str]:
    for path in (("environment",), ("env",), ("metadata", "environment")):
        value = _pluck(config, path)
        if value:
            return value
    return _normalize_string(os.environ.get("COMPASS_ENV") or os.environ.get("BEAMREACH_ENV"))


def _extract_region(config: Dict[str, Any]) -> Optional[str]:
    for path in (
        ("region",),
        ("aws", "region"),
        ("aws", "default_region"),
        ("metadata", "region"),
    ):
        value = _pluck(config, path)
        if value:
            return value
    return _normalize_string(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


@router.get("/api/target")
def get_target_metadata(request: Request) -> Dict[str, Any]:
    workspace_id = getattr(request.state.workspace_ctx, "workspace_id", None)
    if not workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required")
    config = get_workspace_config(request)

    if not isinstance(config, dict):
        raise HTTPException(status_code=500, detail="Target configuration is not a JSON object")

    account_id = _extract_account_id(config)
    account_labels = _extract_account_name(config, workspace_id)
    environment = _extract_environment(config)
    region = _extract_region(config)
    provider = _normalize_string(config.get("provider") or config.get("cloud") or "aws")

    return {
        "target": workspace_id,
        "provider": provider,
        "environment": environment,
        "region": region,
        "account": {
            "id": account_id,
            "name": account_labels.get("name"),
            "alias": account_labels.get("alias"),
        },
    }
