from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).astimezone(timezone.utc)
        except ValueError:
            pass
    return _utcnow()


def _to_iso8601(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_digest(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


@dataclass
class NodeFact:
    org_id: str
    entity_id: str
    node_type: str
    account_id: str
    region: str
    name: str
    tags: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)
    node_class: Optional[str] = None
    canonical_ref: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    confidence: str = "high"
    evidence: List[str] = field(default_factory=list)
    observed_at: datetime = field(default_factory=_utcnow)
    fact_id: Optional[str] = None
    expires_at: Optional[int] = None

    def canonical_fact_id(self) -> str:
        if self.fact_id:
            return self.fact_id
        return _stable_digest(
            {
                "kind": "node_claim",
                "org_id": self.org_id,
                "entity_id": self.entity_id,
                "node_type": self.node_type,
                "account_id": self.account_id,
                "region": self.region,
                "name": self.name,
                "tags": self.tags,
                "attributes": self.attributes,
                "node_class": self.node_class,
                "canonical_ref": self.canonical_ref,
                "aliases": self.aliases,
            }
        )

    def to_item(self, *, source: str, scope: str) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "record_type": "fact",
            "fact_kind": "node_claim",
            "fact_id": self.canonical_fact_id(),
            "org_id": self.org_id,
            "entity_id": self.entity_id,
            "node_type": self.node_type,
            "account_id": self.account_id,
            "region": self.region,
            "name": self.name,
            "tags": self.tags,
            "attributes": self.attributes,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "observed_at": _to_iso8601(self.observed_at),
            "source": source,
            "scope": scope,
        }
        if self.node_class:
            item["node_class"] = self.node_class
        if self.canonical_ref:
            item["canonical_ref"] = self.canonical_ref
        if self.aliases:
            item["aliases"] = list(self.aliases)
        if self.expires_at is not None:
            item["expires_at"] = self.expires_at
        return item

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "NodeFact":
        return cls(
            org_id=item["org_id"],
            entity_id=item["entity_id"],
            node_type=item.get("node_type", ""),
            account_id=item.get("account_id", ""),
            region=item.get("region", ""),
            name=item.get("name", ""),
            tags=item.get("tags", {}),
            attributes=item.get("attributes", {}),
            node_class=item.get("node_class"),
            canonical_ref=item.get("canonical_ref"),
            aliases=item.get("aliases", []),
            confidence=item.get("confidence", "high"),
            evidence=item.get("evidence", []),
            observed_at=_coerce_datetime(item.get("observed_at")),
            fact_id=item.get("fact_id"),
            expires_at=item.get("expires_at"),
        )


@dataclass
class EdgeFact:
    org_id: str
    from_entity_id: str
    to_entity_id: str
    edge_type: str
    confidence: str = "medium"
    evidence: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=_utcnow)
    fact_id: Optional[str] = None
    expires_at: Optional[int] = None

    def canonical_fact_id(self) -> str:
        if self.fact_id:
            return self.fact_id
        return _stable_digest(
            {
                "kind": "edge_claim",
                "org_id": self.org_id,
                "from_entity_id": self.from_entity_id,
                "to_entity_id": self.to_entity_id,
                "edge_type": self.edge_type,
                "attributes": self.attributes,
            }
        )

    def to_item(self, *, source: str, scope: str) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "record_type": "fact",
            "fact_kind": "edge_claim",
            "fact_id": self.canonical_fact_id(),
            "org_id": self.org_id,
            "from_entity_id": self.from_entity_id,
            "to_entity_id": self.to_entity_id,
            "edge_type": self.edge_type,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "attributes": self.attributes,
            "observed_at": _to_iso8601(self.observed_at),
            "source": source,
            "scope": scope,
        }
        if self.expires_at is not None:
            item["expires_at"] = self.expires_at
        return item

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "EdgeFact":
        return cls(
            org_id=item["org_id"],
            from_entity_id=item.get("from_entity_id", ""),
            to_entity_id=item.get("to_entity_id", ""),
            edge_type=item.get("edge_type", ""),
            confidence=item.get("confidence", "medium"),
            evidence=item.get("evidence", []),
            attributes=item.get("attributes", {}),
            observed_at=_coerce_datetime(item.get("observed_at")),
            fact_id=item.get("fact_id"),
            expires_at=item.get("expires_at"),
        )


@dataclass
class FactBatch:
    org_id: str
    source: str
    scope: str
    node_facts: List[NodeFact] = field(default_factory=list)
    edge_facts: List[EdgeFact] = field(default_factory=list)
