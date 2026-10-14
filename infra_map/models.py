from __future__ import annotations

import hashlib
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


@dataclass
class TerraformMetadata:
    resource_address: str
    repo: Optional[str] = None
    state_file: Optional[str] = None
    module_path: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"resource_address": self.resource_address}
        optional_fields = ("repo", "state_file", "module_path", "branch", "commit")
        for field_name in optional_fields:
            value = getattr(self, field_name)
            if value:
                data[field_name] = value
        return data

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]) -> Optional["TerraformMetadata"]:
        if not payload:
            return None
        return cls(
            resource_address=payload.get("resource_address", ""),
            repo=payload.get("repo"),
            state_file=payload.get("state_file"),
            module_path=payload.get("module_path"),
            branch=payload.get("branch"),
            commit=payload.get("commit"),
        )


@dataclass
class ResourceNode:
    org_id: str
    resource_id: str
    type: str
    account_id: str
    region: str
    name: str
    tags: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)
    node_class: Optional[str] = None
    canonical_ref: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    confidence: str = "high"
    source: Optional[str] = None
    scope: Optional[str] = None
    last_seen: datetime = field(default_factory=_utcnow)
    terraform: Optional[TerraformMetadata] = None
    expires_at: Optional[int] = None

    def to_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "org_id": self.org_id,
            "resource_id": self.resource_id,
            "resource_type": self.type,
            "account_id": self.account_id,
            "region": self.region,
            "name": self.name,
            "tags": self.tags,
            "attributes": self.attributes,
            "confidence": self.confidence,
            "last_seen": _to_iso8601(self.last_seen),
            "record_type": "resource",
        }
        if self.node_class:
            item["node_class"] = self.node_class
        if self.canonical_ref:
            item["canonical_ref"] = self.canonical_ref
        if self.aliases:
            item["aliases"] = list(self.aliases)
        if self.source:
            item["source"] = self.source
        if self.scope:
            item["scope"] = self.scope
        if self.terraform:
            item["terraform"] = self.terraform.to_dict()
        if self.expires_at is not None:
            item["expires_at"] = self.expires_at
        return item

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "ResourceNode":
        return cls(
            org_id=item["org_id"],
            resource_id=item["resource_id"],
            type=item.get("resource_type", ""),
            account_id=item.get("account_id", ""),
            region=item.get("region", ""),
            name=item.get("name", item.get("resource_name", "")),
            tags=item.get("tags", {}),
            attributes=item.get("attributes", {}),
            node_class=item.get("node_class"),
            canonical_ref=item.get("canonical_ref"),
            aliases=item.get("aliases", []),
            confidence=item.get("confidence", "high"),
            source=item.get("source"),
            scope=item.get("scope"),
            last_seen=_coerce_datetime(item.get("last_seen")),
            terraform=TerraformMetadata.from_dict(item.get("terraform")),
            expires_at=item.get("expires_at"),
        )

    def touch(self, observed_at: Optional[datetime] = None) -> None:
        self.last_seen = _coerce_datetime(observed_at or _utcnow())


@dataclass
class RelationshipEdge:
    org_id: str
    from_resource_id: str
    to_resource_id: str
    relationship_type: str
    confidence: str = "medium"
    evidence: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None
    scope: Optional[str] = None
    last_seen: datetime = field(default_factory=_utcnow)
    edge_id: Optional[str] = None
    expires_at: Optional[int] = None

    def canonical_edge_id(self) -> str:
        if self.edge_id:
            return self.edge_id
        base = f"{self.from_resource_id}->{self.to_resource_id}|{self.relationship_type}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def to_attributes(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "org_id": self.org_id,
            "from_resource_id": self.from_resource_id,
            "to_resource_id": self.to_resource_id,
            "relationship_type": self.relationship_type,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "attributes": self.attributes,
            "last_seen": _to_iso8601(self.last_seen),
            "edge_id": self.canonical_edge_id(),
            "record_type": "relationship",
            "resource_type": "relationship",
            "target_resource_id": self.to_resource_id,
        }
        if self.source:
            data["source"] = self.source
        if self.scope:
            data["scope"] = self.scope
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at
        return data

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "RelationshipEdge":
        return cls(
            org_id=item["org_id"],
            from_resource_id=item["from_resource_id"],
            to_resource_id=item["to_resource_id"],
            relationship_type=item["relationship_type"],
            confidence=item.get("confidence", "medium"),
            evidence=item.get("evidence", []),
            attributes=item.get("attributes", {}),
            source=item.get("source"),
            scope=item.get("scope"),
            last_seen=_coerce_datetime(item.get("last_seen")),
            edge_id=item.get("edge_id"),
            expires_at=item.get("expires_at"),
        )
