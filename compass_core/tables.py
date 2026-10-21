"""The one place that knows which `dynamodb_config` key names which table.

`workspace/context.py` fills the keys in from the environment; this module
reads them. The two fallback rules that used to be repeated at every call site
live here: the items table defaults to the audit table, and the map table has
three legacy spellings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from compass_core import sessions


def _dynamodb_config(config: Any) -> Dict[str, Any]:
    """Accepts a workspace config (with a `dynamodb_config` block) or the block itself."""
    if not isinstance(config, Mapping):
        return {}
    nested = config.get("dynamodb_config")
    if isinstance(nested, Mapping):
        return dict(nested)
    if "dynamodb_config" in config:
        return {}
    return dict(config)


@dataclass(frozen=True)
class Tables:
    audit: Optional[str]
    items: Optional[str]
    map: Optional[str]
    progress: Optional[str]
    jobs: Optional[str]
    ropes: Optional[str]
    policies: Optional[str]
    aws_inventory: Optional[str]
    tf_inventory: Optional[str]
    tf_state: Optional[str]
    workspace_id: Optional[str]
    org_id: Optional[str]

    @classmethod
    def from_config(cls, config: Any) -> "Tables":
        cfg = _dynamodb_config(config)

        def first(*keys: str) -> Optional[str]:
            for key in keys:
                value = cfg.get(key)
                if value:
                    return str(value)
            return None

        return cls(
            audit=first("audit"),
            items=first("items", "audit"),
            map=first("map", "resource_map", "resource_map_table", "table_name"),
            progress=first("progress"),
            jobs=first("jobs"),
            ropes=first("ropes"),
            policies=first("policies"),
            aws_inventory=first("aws_inventory"),
            tf_inventory=first("tf_inventory"),
            tf_state=first("tf_state"),
            workspace_id=first("workspace_id"),
            org_id=first("org_id"),
        )


def tables(config: Any) -> Tables:
    return Tables.from_config(config)


def dynamo_table(name: str):
    """A boto3 Table in the configured region, the way every route and captain builds one."""

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    return sessions.control(region=region).resource("dynamodb").Table(name)
