"""A honeytoken's placement: where it was planted, read back from the token item."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from compass_core.keys import honeytoken_sort_key


def to_native(value: Any) -> Any:
    """DynamoDB Decimals back to int/float, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [to_native(v) for v in value]
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    return value


def get_placement(table: Any, pk: str, honeytoken_id: str) -> Optional[Dict[str, Any]]:
    """The token's single placement (embedded on the token item), or None."""
    try:
        item = table.get_item(Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)}).get("Item") or {}
    except Exception:
        return None
    return to_native(item.get("placement")) or None


def placement_summary(placement: Optional[Dict[str, Any]]) -> str:
    """One-line breach-source attribution from a honeytoken's placement."""
    if not placement:
        return ""
    ptype = str(placement.get("type") or "").replace("_", " ")
    resource = str(placement.get("resource_arn") or placement.get("detail") or "").strip()
    source = str(placement.get("service_arn") or placement.get("detail") or resource).strip()
    planted = " ".join(p for p in (ptype, resource) if p)
    if not planted:
        return ""
    return f"Planted in {planted} — likely breach source: {source}." if source else f"Planted in {planted}."
