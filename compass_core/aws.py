"""How the target account is configured: regions, static keys, the roles to assume. Sessions
themselves are built by ``compass_core.sessions``."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _aws_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    aws_cfg = config.get("aws")
    return aws_cfg if isinstance(aws_cfg, dict) else {}


def _local_iam_mode(config: Any) -> bool:
    aws_cfg = _aws_config(config)
    auth_mode = aws_cfg.get("auth_mode")
    return isinstance(auth_mode, str) and auth_mode.strip().lower() == "local_iam"


def _local_iam_runtime_enabled() -> bool:
    # Opt-in for single-tenant/local deployments where the ambient credentials
    # (task role, ~/.aws) belong to the target account. Must stay off in the
    # shared multi-tenant service, where the ambient identity is the control
    # plane's, not the tenant's.
    # COMPASS_ALLOW_LOCAL_IAM; the older BEAMREACH_ name still works
    return (os.getenv("COMPASS_ALLOW_LOCAL_IAM") or os.getenv("BEAMREACH_ALLOW_LOCAL_IAM") or "").strip().lower() in {"1", "true", "yes"}


def resolve_target_region(config: Any, default: Optional[str] = None) -> Optional[str]:
    aws_cfg = _aws_config(config)
    region = (
        aws_cfg.get("region")
        or aws_cfg.get("default_region")
        or (config.get("aws_region") if isinstance(config, dict) else None)
        or default
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
    )
    if isinstance(region, str):
        region = region.strip()
    return region or None


def resolve_static_credentials_config(config: Any, capability: str = "read") -> Optional[Dict[str, Any]]:
    aws_cfg = _aws_config(config)

    candidates = []
    if capability == "respond":
        # Explicit only; see resolve_assume_role_config.
        candidate = aws_cfg.get("credentials_respond") or aws_cfg.get("static_credentials_respond")
        return candidate if isinstance(candidate, dict) else None
    if capability == "write":
        candidates.extend(
            [
                aws_cfg.get("credentials_write"),
                aws_cfg.get("static_credentials_write"),
                aws_cfg.get("write_credentials"),
            ]
        )
    else:
        candidates.extend(
            [
                aws_cfg.get("credentials_read"),
                aws_cfg.get("static_credentials_read"),
                aws_cfg.get("read_credentials"),
            ]
        )
    candidates.extend(
        [
            aws_cfg.get("credentials"),
            aws_cfg.get("static_credentials"),
            config.get("credentials") if isinstance(config, dict) else None,
        ]
    )

    for candidate in candidates:
        if isinstance(candidate, dict) and (
            candidate.get("access_key_id")
            or candidate.get("aws_access_key_id")
            or candidate.get("secret_access_key")
            or candidate.get("aws_secret_access_key")
        ):
            return candidate
    return None


def resolve_assume_role_config(config: Any, capability: str = "read") -> Optional[Dict[str, Any]]:
    if not isinstance(config, dict):
        return None

    aws_cfg = _aws_config(config)
    assume_cfg: Optional[Dict[str, Any]] = None
    if capability == "respond":
        # The honeypot responder role is scoped to the whitelisted actions and
        # must be configured explicitly: never fall back to the read or write
        # role, which would let a response action run with broader rights.
        assume_cfg = aws_cfg.get("assume_role_respond") or config.get("assume_role_respond")
        return assume_cfg if isinstance(assume_cfg, dict) else None
    if capability == "write":
        assume_cfg = aws_cfg.get("assume_role_write") or aws_cfg.get("assume_role")
    else:
        assume_cfg = aws_cfg.get("assume_role_read") or aws_cfg.get("assume_role")

    if not assume_cfg:
        top_level_key = "assume_role_write" if capability == "write" else "assume_role"
        assume_cfg = config.get(top_level_key) or config.get("assume_role")

    return assume_cfg if isinstance(assume_cfg, dict) else None


def resolve_target_account_id(config: Any, capability: str = "read") -> Optional[str]:
    aws_cfg = _aws_config(config)

    for candidate in (
        aws_cfg.get("account_id"),
        (resolve_assume_role_config(config, capability) or {}).get("account_id"),
        (resolve_static_credentials_config(config, capability) or {}).get("account_id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def has_target_aws_access(config: Any, capability: str = "read") -> bool:
    return bool(
        resolve_static_credentials_config(config, capability)
        or resolve_assume_role_config(config, capability)
        or (_local_iam_mode(config) and _local_iam_runtime_enabled())
    )
