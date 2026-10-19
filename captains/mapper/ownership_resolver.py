from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from infra_map.facts import EdgeFact
from infra_map.graph_store import GraphStore
from infra_map.models import RelationshipEdge, ResourceNode

from .infra_map_ids import state_file_scope, tf_address_node_id


def build_derived_correspondence_edges(
    org_id: str,
    nodes: Sequence[ResourceNode],
    edges: Sequence[RelationshipEdge],
) -> List[RelationshipEdge]:
    node_by_id = {node.resource_id: node for node in nodes}
    writes_by_state: Dict[str, List[str]] = defaultdict(list)
    existing = {
        (edge.from_resource_id, edge.to_resource_id, edge.relationship_type)
        for edge in edges
    }
    for edge in edges:
        if edge.relationship_type == "writes":
            writes_by_state[edge.to_resource_id].append(edge.from_resource_id)

    derived: List[RelationshipEdge] = []
    for node in nodes:
        if node.type != "tf_state_instance":
            continue
        state_file_id = (node.attributes or {}).get("state_file_id")
        resource_address = (node.attributes or {}).get("resource_address")
        if not isinstance(state_file_id, str) or not isinstance(resource_address, str):
            continue
        for tf_root_module_id in writes_by_state.get(state_file_id, []):
            tf_address_id = tf_address_node_id(tf_root_module_id, resource_address)
            if tf_address_id not in node_by_id:
                continue
            edge_key = (node.resource_id, tf_address_id, "corresponds_to")
            if edge_key in existing:
                continue
            derived.append(
                RelationshipEdge(
                    org_id=org_id,
                    from_resource_id=node.resource_id,
                    to_resource_id=tf_address_id,
                    relationship_type="corresponds_to",
                    confidence="high",
                    evidence=["derived:state+root_module+git_address"],
                    attributes={
                        "tf_root_module_id": tf_root_module_id,
                        "resource_address": resource_address,
                    },
                    source="derived",
                    scope=state_file_scope(state_file_id),
                )
            )
    return derived


def build_derived_correspondence_edge_facts(
    org_id: str,
    nodes: Sequence[ResourceNode],
    edges: Sequence[RelationshipEdge],
) -> List[Tuple[str, EdgeFact]]:
    facts: List[Tuple[str, EdgeFact]] = []
    for edge in build_derived_correspondence_edges(org_id, nodes, edges):
        facts.append(
            (
                edge.scope or "derived:global",
                EdgeFact(
                    org_id=edge.org_id,
                    from_entity_id=edge.from_resource_id,
                    to_entity_id=edge.to_resource_id,
                    edge_type=edge.relationship_type,
                    confidence=edge.confidence,
                    evidence=list(edge.evidence),
                    attributes=dict(edge.attributes),
                    observed_at=edge.last_seen,
                    expires_at=edge.expires_at,
                ),
            )
        )
    return facts


class InfraMapOwnershipResolver:
    def __init__(self, store: GraphStore):
        self.store = store

    def resolve_ownership(
        self,
        org_id: str,
        resource_id: str,
        *,
        max_candidates: int = 3,
    ) -> Dict:
        graph = self.store.fetch_graph(org_id)
        nodes = {node.resource_id: node for node in graph.get("nodes", [])}
        incoming: Dict[str, List[RelationshipEdge]] = defaultdict(list)
        outgoing: Dict[str, List[RelationshipEdge]] = defaultdict(list)
        for edge in graph.get("edges", []):
            outgoing[edge.from_resource_id].append(edge)
            incoming[edge.to_resource_id].append(edge)

        candidates = self._collect_candidates(resource_id, nodes, incoming, outgoing)
        if not candidates:
            return {
                "status": "unknown",
                "resource_id": resource_id,
                "answer": None,
                "candidates": [],
                "evidence": [],
                "path": [],
                "freshness": self._freshness_payload(None, None, None, None),
                "ambiguity_notes": [],
                "missing_data_notes": ["No ownership path found in infra_map"],
            }

        candidates.sort(key=lambda item: item["score"], reverse=True)
        top = candidates[0]
        status = "verified"
        ambiguity_notes: List[str] = []
        if len(candidates) > 1 and abs(candidates[0]["score"] - candidates[1]["score"]) < 0.1:
            status = "ambiguous"
            ambiguity_notes.append("Multiple ownership paths scored similarly.")
        elif not top.get("verified"):
            status = "partial"

        answer = {
            "repo": top.get("repo"),
            "file": top.get("file"),
            "resource_address": top.get("resource_address"),
            "module_path": top.get("module_path"),
            "tf_root_module_id": top.get("tf_root_module_id"),
            "tf_state_file_id": top.get("tf_state_file_id"),
            "account_id": top.get("account_id"),
            "region": top.get("region"),
        }
        return {
            "status": status,
            "resource_id": resource_id,
            "answer": answer,
            "candidates": candidates[:max_candidates],
            "evidence": top.get("evidence", []),
            "path": top.get("path", []),
            "freshness": top.get("freshness"),
            "ambiguity_notes": ambiguity_notes,
            "missing_data_notes": top.get("missing_data_notes", []),
        }

    def _collect_candidates(
        self,
        resource_id: str,
        nodes: Dict[str, ResourceNode],
        incoming: Dict[str, List[RelationshipEdge]],
        outgoing: Dict[str, List[RelationshipEdge]],
    ) -> List[Dict]:
        resource = nodes.get(resource_id)
        observe_edges = [
            edge
            for edge in incoming.get(resource_id, [])
            if edge.relationship_type == "observes"
        ]
        candidates: List[Dict] = []
        for observe_edge in observe_edges:
            instance = nodes.get(observe_edge.from_resource_id)
            if not instance:
                continue
            state_file_edges = [
                edge
                for edge in incoming.get(instance.resource_id, [])
                if edge.relationship_type == "contains"
            ]
            address_edges = [
                edge
                for edge in outgoing.get(instance.resource_id, [])
                if edge.relationship_type == "corresponds_to"
            ]
            if not state_file_edges:
                continue
            for state_edge in state_file_edges:
                state_file = nodes.get(state_edge.from_resource_id)
                write_edges = [
                    edge
                    for edge in incoming.get(state_edge.from_resource_id, [])
                    if edge.relationship_type == "writes"
                ]
                for write_edge in write_edges:
                    tf_root = nodes.get(write_edge.from_resource_id)
                    if not tf_root:
                        continue
                    address_candidates = list(address_edges)
                    if not address_candidates:
                        fallback_address_id = tf_address_node_id(
                            tf_root.resource_id,
                            (instance.attributes or {}).get("resource_address", ""),
                        )
                        if fallback_address_id in nodes:
                            address_candidates = [
                                RelationshipEdge(
                                    org_id=instance.org_id,
                                    from_resource_id=instance.resource_id,
                                    to_resource_id=fallback_address_id,
                                    relationship_type="corresponds_to",
                                    confidence="medium",
                                    evidence=["resolver:fallback"],
                                )
                            ]
                    if not address_candidates:
                        candidates.append(
                            self._candidate_payload(
                                resource=resource,
                                instance=instance,
                                state_file=state_file,
                                tf_root=tf_root,
                                tf_address=None,
                                github_file=None,
                                github_repo=None,
                                verified=False,
                                reasons=["exact state observation", "root module found, but no declaring file found"],
                                missing_data_notes=["No tf_address declaration found for the state-backed resource address"],
                            )
                        )
                        continue
                    for address_edge in address_candidates:
                        tf_address = nodes.get(address_edge.to_resource_id)
                        file_edges = [
                            edge
                            for edge in incoming.get(address_edge.to_resource_id, [])
                            if edge.relationship_type == "declares"
                        ]
                        if not file_edges:
                            candidates.append(
                                self._candidate_payload(
                                    resource=resource,
                                    instance=instance,
                                    state_file=state_file,
                                    tf_root=tf_root,
                                    tf_address=tf_address,
                                    github_file=None,
                                    github_repo=None,
                                    verified=False,
                                    reasons=["exact state observation", "exact terraform address found, but no declaring file found"],
                                    missing_data_notes=["No declaring github_file found for tf_address"],
                                )
                            )
                            continue
                        for file_edge in file_edges:
                            github_file = nodes.get(file_edge.from_resource_id)
                            if not github_file:
                                continue
                            part_of = any(
                                edge.relationship_type == "part_of" and edge.to_resource_id == tf_root.resource_id
                                for edge in outgoing.get(github_file.resource_id, [])
                            )
                            repo_edges = [
                                edge
                                for edge in incoming.get(github_file.resource_id, [])
                                if edge.relationship_type == "contains"
                            ]
                            github_repo = nodes.get(repo_edges[0].from_resource_id) if repo_edges else None
                            reasons = ["exact state observation", "exact scoped terraform address declaration"]
                            verified = bool(part_of and github_repo and tf_address)
                            if part_of:
                                reasons.append("matching root module")
                            else:
                                reasons.append("declaring file was not linked to the same root module")
                            candidates.append(
                                self._candidate_payload(
                                    resource=resource,
                                    instance=instance,
                                    state_file=state_file,
                                    tf_root=tf_root,
                                    tf_address=tf_address,
                                    github_file=github_file,
                                    github_repo=github_repo,
                                    verified=verified,
                                    reasons=reasons,
                                    missing_data_notes=[] if verified else ["Declaring file/root module linkage is incomplete"],
                                )
                            )
        return candidates

    def _candidate_payload(
        self,
        *,
        resource: Optional[ResourceNode],
        instance: ResourceNode,
        state_file: Optional[ResourceNode],
        tf_root: Optional[ResourceNode],
        tf_address: Optional[ResourceNode],
        github_file: Optional[ResourceNode],
        github_repo: Optional[ResourceNode],
        verified: bool,
        reasons: Sequence[str],
        missing_data_notes: Sequence[str],
    ) -> Dict:
        score = 0.5
        if tf_root:
            score += 0.15
        if tf_address:
            score += 0.2
        if github_file:
            score += 0.1
        if github_repo:
            score += 0.05
        if not verified:
            score -= 0.1

        repo_name = github_repo.name if github_repo else None
        file_path = (github_file.attributes or {}).get("path") if github_file else None
        resource_address = (instance.attributes or {}).get("resource_address")
        module_path = (instance.attributes or {}).get("module_path")
        freshness = self._freshness_payload(resource, instance, state_file, github_file)
        path = [
            {"node_id": resource.resource_id if resource else (instance.attributes or {}).get("aws_resource_id")},
            {"edge_type": "observes", "direction": "incoming"},
            {"node_id": instance.resource_id},
        ]
        if tf_address:
            path.extend(
                [
                    {"edge_type": "corresponds_to", "direction": "outgoing"},
                    {"node_id": tf_address.resource_id},
                ]
            )
        if github_file:
            path.extend(
                [
                    {"edge_type": "declares", "direction": "incoming"},
                    {"node_id": github_file.resource_id},
                ]
            )
        evidence = [
            {
                "type": "tf_state_observation",
                "source": instance.source or "tf_state",
                "value": instance.resource_id,
                "confidence": "high",
            }
        ]
        if tf_address:
            evidence.append(
                {
                    "type": "tf_address_match",
                    "source": tf_address.source or "git_scan",
                    "value": tf_address.resource_id,
                    "confidence": "high" if verified else "medium",
                }
            )
        return {
            "repo": repo_name,
            "file": file_path,
            "resource_address": resource_address,
            "module_path": module_path,
            "tf_root_module_id": tf_root.resource_id if tf_root else None,
            "tf_state_file_id": state_file.resource_id if state_file else None,
            "account_id": resource.account_id if resource else None,
            "region": resource.region if resource else None,
            "confidence": "high" if verified else "medium",
            "score": round(score, 3),
            "verified": verified,
            "reasons": list(reasons),
            "evidence": evidence,
            "freshness": freshness,
            "path": path,
            "missing_data_notes": list(missing_data_notes),
        }

    def _freshness_payload(
        self,
        resource: Optional[ResourceNode],
        instance: Optional[ResourceNode],
        state_file: Optional[ResourceNode],
        github_file: Optional[ResourceNode],
    ) -> Dict:
        now = datetime.now(timezone.utc)
        aws_seen_at = resource.last_seen if resource else None
        tf_state_seen_at = state_file.last_seen if state_file else (instance.last_seen if instance else None)
        git_seen_at = github_file.last_seen if github_file else None
        timestamps = [ts for ts in [aws_seen_at, tf_state_seen_at, git_seen_at] if ts is not None]
        stale = False
        if aws_seen_at and now - aws_seen_at > timedelta(hours=1):
            stale = True
        if tf_state_seen_at and now - tf_state_seen_at > timedelta(hours=6):
            stale = True
        if git_seen_at and now - git_seen_at > timedelta(hours=24):
            stale = True
        return {
            "resolved_at": now.isoformat().replace("+00:00", "Z"),
            "aws_seen_at": aws_seen_at.isoformat().replace("+00:00", "Z") if aws_seen_at else None,
            "tf_state_seen_at": tf_state_seen_at.isoformat().replace("+00:00", "Z") if tf_state_seen_at else None,
            "git_seen_at": git_seen_at.isoformat().replace("+00:00", "Z") if git_seen_at else None,
            "stale": stale,
            "freshness_class": "stale" if stale else "fresh",
            "critical_observation_count": len(timestamps),
        }
