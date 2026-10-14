from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import boto3


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


def build_target_session(
    config: Any,
    *,
    capability: str = "read",
    region: Optional[str] = None,
    session_name_prefix: str = "beamreach-target",
    allow_default: bool = False,
):
    resolved_region = resolve_target_region(config, default=region)
    static_cfg = resolve_static_credentials_config(config, capability)
    if static_cfg:
        return boto3.Session(
            aws_access_key_id=static_cfg.get("access_key_id") or static_cfg.get("aws_access_key_id"),
            aws_secret_access_key=static_cfg.get("secret_access_key") or static_cfg.get("aws_secret_access_key"),
            aws_session_token=static_cfg.get("session_token") or static_cfg.get("aws_session_token"),
            region_name=resolved_region,
        )

    assume_cfg = resolve_assume_role_config(config, capability)
    if assume_cfg:
        account_id = assume_cfg.get("account_id")
        role_name = assume_cfg.get("role_name")
        if not account_id or not role_name:
            raise KeyError("assume_role config requires account_id and role_name")

        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        external_id = assume_cfg.get("external_id")
        session_name = assume_cfg.get("session_name") or f"{session_name_prefix}-{capability}-{int(time.time())}"
        sts_client = boto3.client("sts", region_name=resolved_region)
        assume_kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
        }
        if external_id:
            assume_kwargs["ExternalId"] = external_id
        response = sts_client.assume_role(**assume_kwargs)
        creds = response["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=resolved_region,
        )

    if allow_default or (_local_iam_mode(config) and _local_iam_runtime_enabled()):
        return boto3.Session(region_name=resolved_region)
    return None


def target_credentials_env(
    config: Any,
    capability: str = "read",
    region: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Target-account credentials as environment variables for a subprocess.

    Tools that are not boto3 — terraform, in particular — cannot be handed a
    `boto3.Session`. They read the standard AWS environment variables instead,
    so the session's credentials are frozen and passed across that way. For an
    assumed role this yields the temporary key, secret and session token, which
    is exactly what a provider block with no explicit `profile` will pick up.

    `AWS_PROFILE` maps to None so the caller can *remove* it. Leaving the
    worker's own profile in place would silently win over these variables and
    point terraform back at the control-plane account — which is the account the
    resources being imported are definitively not in.
    """
    session = build_target_session(config, capability=capability, region=region)
    if session is None:
        return {}

    credentials = session.get_credentials()
    if credentials is None:
        return {}
    frozen = credentials.get_frozen_credentials()

    env: Dict[str, Optional[str]] = {
        "AWS_ACCESS_KEY_ID": frozen.access_key,
        "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        "AWS_PROFILE": None,
        "AWS_SESSION_TOKEN": frozen.token or None,
    }
    resolved_region = session.region_name or resolve_target_region(config, default=region)
    if resolved_region:
        env["AWS_REGION"] = resolved_region
        env["AWS_DEFAULT_REGION"] = resolved_region
    return env
