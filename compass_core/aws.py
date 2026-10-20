"""How the target account is configured, read from the workspace config's ``aws`` block
(docs/specs/access-model.md §12). Sessions themselves are built by ``compass_core.sessions``.

    aws:
      account_id, region, regions[]           the target
      roles: {read, act}                      the two roles `compass connect` created
      external_id, session_prefix, source_identity
      install_id                              names this installation's roles and runtime user
      state_buckets[]                         the Terraform state the mapper may read
      control: {account_id, credential_source, runtime_arn, credentials, alert_queues}
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def _aws_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    aws_cfg = config.get("aws")
    return aws_cfg if isinstance(aws_cfg, dict) else {}


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


def resolve_target_regions(config: Any) -> List[str]:
    """Every region the target is watched in: ``aws.regions``, else the one region."""
    aws_cfg = _aws_config(config)
    regions = aws_cfg.get("regions") if isinstance(aws_cfg.get("regions"), list) else []
    found = [str(r).strip() for r in regions if str(r).strip()]
    default = resolve_target_region(config)
    if default and default not in found:
        found.insert(0, default)
    return found


def resolve_target_account_id(config: Any, capability: str = "read") -> Optional[str]:
    """The target account: ``aws.account_id``, else the account in the role ARN."""
    aws_cfg = _aws_config(config)
    account = aws_cfg.get("account_id")
    if isinstance(account, str) and account.strip():
        return account.strip()
    arn = role_arn(config, "act" if capability in ("act", "respond") else "read") or role_arn(config, "read")
    return arn.split(":")[4] if arn and arn.count(":") >= 5 else None


def roles(config: Any) -> Dict[str, str]:
    found = _aws_config(config).get("roles")
    return {k: str(v) for k, v in found.items() if isinstance(v, str) and v.strip()} if isinstance(found, dict) else {}


def role_arn(config: Any, capability: str) -> Optional[str]:
    """The role a capability assumes: ``read`` → ``aws.roles.read``; ``act`` (or ``respond``)
    → ``aws.roles.act``, explicit and never a fallback to the read role."""
    key = "act" if capability in ("act", "respond") else "read"
    return roles(config).get(key)


def external_id(config: Any) -> Optional[str]:
    value = _aws_config(config).get("external_id")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def control_config(config: Any) -> Dict[str, Any]:
    found = _aws_config(config).get("control")
    return found if isinstance(found, dict) else {}


def alert_queues(config: Any) -> Dict[str, Dict[str, str]]:
    """region → {arn, url} of the decoy alert queues: ``aws.control.alert_queues`` once
    `connect` has run, else the queue `init_alerts` created in the target account under the
    decoy captain's own keys (honoured for one release)."""
    found = control_config(config).get("alert_queues")
    if isinstance(found, dict) and found:
        return {str(r): {"arn": str(q.get("arn") or ""), "url": str(q.get("url") or "")} for r, q in found.items() if isinstance(q, dict)}
    legacy = config.get("honeypot") if isinstance(config, dict) and isinstance(config.get("honeypot"), dict) else {}
    arn, url = legacy.get("alert_queue_arn"), legacy.get("alert_queue_url")
    if isinstance(arn, str) and arn.count(":") >= 5:
        return {arn.split(":")[3]: {"arn": arn, "url": str(url or "")}}
    return {}


def has_target_aws_access(config: Any, capability: str = "read") -> bool:
    """Whether `compass connect` has run: the role for the capability is configured."""
    return bool(role_arn(config, capability))
