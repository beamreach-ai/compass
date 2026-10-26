"""Where advisories live: rows ``CVE#<id>`` in the items table, with an alias row per other
id so a GHSA or a Packagist id finds its CVE, and one cursor row per feed
(docs/specs/patching-captain.md §3.2)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from boto3.dynamodb.conditions import Key

from captains.patch.advisories import Advisory, merge
from compass_core.keys import advisory_alias_sort_key, advisory_prefix, advisory_sort_key, patch_cursor_prefix, patch_cursor_sort_key


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    return value


class AdvisoryStore:
    def __init__(self, table: Any, pk: str):
        self.table = table
        self.pk = pk

    # ── advisories ──
    def get(self, advisory_id: str) -> Optional[Advisory]:
        row = self.table.get_item(Key={"pk": self.pk, "sk": advisory_sort_key(advisory_id)}).get("Item")
        if row is None:
            alias = self.table.get_item(Key={"pk": self.pk, "sk": advisory_alias_sort_key(advisory_id)}).get("Item")
            if alias is None:
                return None
            row = self.table.get_item(Key={"pk": self.pk, "sk": advisory_sort_key(str(alias["advisory_id"]))}).get("Item")
            if row is None:
                return None
        return Advisory.from_dict(_to_native(row["record"]))

    def put(self, advisory: Advisory) -> Advisory:
        """Merge ``advisory`` into what is stored under any of its ids and write the result."""
        existing = None
        for candidate in advisory.all_ids:
            existing = self.get(candidate)
            if existing is not None:
                break
        merged = merge(existing, advisory)
        if existing is not None and existing.id != merged.id:
            self.table.delete_item(Key={"pk": self.pk, "sk": advisory_sort_key(existing.id)})
        self.table.put_item(Item={"pk": self.pk, "sk": advisory_sort_key(merged.id), "advisory_id": merged.id, "modified": merged.modified or "",
                                  "cvss": _to_dynamo(merged.cvss), "kev": bool(merged.kev), "record": _to_dynamo(merged.to_dict()), "updated_at": int(time.time() * 1000)})
        for alias in merged.aliases:
            self.table.put_item(Item={"pk": self.pk, "sk": advisory_alias_sort_key(alias), "advisory_id": merged.id})
        return merged

    def list(self, *, limit: Optional[int] = None) -> List[Advisory]:
        out: List[Advisory] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").begins_with(advisory_prefix())}
        while True:
            page = self.table.query(**kwargs)
            for row in page.get("Items", []):
                if "record" in row:
                    out.append(Advisory.from_dict(_to_native(row["record"])))
                    if limit and len(out) >= limit:
                        return out
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def ids(self) -> List[str]:
        out: List[str] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").begins_with(advisory_prefix()),
                                  "ProjectionExpression": "sk, advisory_id"}
        while True:
            page = self.table.query(**kwargs)
            out.extend(str(r["advisory_id"]) for r in page.get("Items", []) if r.get("advisory_id") and not str(r["sk"]).startswith(advisory_prefix() + "ALIAS#"))
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def all_ids(self) -> List[str]:
        """Every id the store answers for: the canonical ones and their aliases."""
        out: List[str] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").begins_with(advisory_prefix()),
                                  "ProjectionExpression": "sk, advisory_id"}
        alias_prefix = advisory_prefix() + "ALIAS#"
        while True:
            page = self.table.query(**kwargs)
            for row in page.get("Items", []):
                sk = str(row["sk"])
                out.append(sk[len(alias_prefix):] if sk.startswith(alias_prefix) else str(row.get("advisory_id") or sk[len(advisory_prefix()):]))
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def count(self) -> int:
        return len(self.ids())

    # ── cursors ──
    def cursor(self, source: str) -> Optional[Dict[str, Any]]:
        row = self.table.get_item(Key={"pk": self.pk, "sk": patch_cursor_sort_key(source)}).get("Item")
        return _to_native({k: v for k, v in row.items() if k not in ("pk", "sk")}) if row else None

    def set_cursor(self, source: str, value: Any, **fields: Any) -> Dict[str, Any]:
        row = {"pk": self.pk, "sk": patch_cursor_sort_key(source), "source": source, "value": _to_dynamo(value), "updated_at": int(time.time() * 1000),
               **_to_dynamo(fields)}
        self.table.put_item(Item=row)
        return _to_native({k: v for k, v in row.items() if k not in ("pk", "sk")})

    def cursors(self) -> Dict[str, Dict[str, Any]]:
        page = self.table.query(KeyConditionExpression=Key("pk").eq(self.pk) & Key("sk").begins_with(patch_cursor_prefix()))
        return {str(r["source"]): _to_native({k: v for k, v in r.items() if k not in ("pk", "sk")}) for r in page.get("Items", []) if r.get("source")}

    def put_many(self, advisories: Iterable[Advisory]) -> int:
        n = 0
        for advisory in advisories:
            self.put(advisory)
            n += 1
        return n
