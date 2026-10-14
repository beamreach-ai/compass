"""Honeytoken class ids and the aliases that keep old stored items loading.

Class ids name what the token *is*, not what it is for.
"""
from __future__ import annotations

from typing import Any

from compass_core.legacy_honeytoken_ids import LEGACY_CLASS_ALIASES as _LEGACY

HONEYTOKEN_CLASSES = {
    "aws_credential",
    "db_credential",
    "ssh_key",
    "s3_object",
    "api_token",
    "dns_record",
}

# Ids written by earlier releases. Stored items may still carry them until
# the ids have been migrated.
LEGACY_CLASS_ALIASES = _LEGACY


def normalize_class(value: Any) -> str:
    """Map a legacy class id onto its current one; anything else passes through."""
    text = str(value or "").strip()
    return LEGACY_CLASS_ALIASES.get(text, text)
