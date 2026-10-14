from __future__ import annotations

import fnmatch
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from infra_map.models import RelationshipEdge, ResourceNode
from infra_map.store import InfraMapStore


class InfraMapLibrarian:
    """Deterministic query layer over infra_map for agent-facing answers."""

    def __init__(self, store: InfraMapStore):
        self.store = store

    def get_terraform_location(
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

        candidates: List[Dict] = []
        for matched_resource_id in self._resource_ids_for_lookup(resource_id, nodes):
            candidates.extend(
                self._collect_terraform_candidates(
                    matched_resource_id,
                    nodes,
                    incoming,
                    outgoing,
                )
            )
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
                "missing_data_notes": ["No Terraform location path found in infra_map"],
            }

        candidates.sort(key=lambda item: item["score"], reverse=True)
        top = candidates[0]
        status = "verified"
        ambiguity_notes: List[str] = []
        if len(candidates) > 1 and abs(candidates[0]["score"] - candidates[1]["score"]) < 0.1:
            status = "ambiguous"
            ambiguity_notes.append("Multiple Terraform locations scored similarly.")
        elif not top.get("verified"):
            status = "partial"

        answer = {
            "repo": top.get("repo"),
            "file": top.get("file"),
            "tf_root_module": top.get("tf_root_module"),
            "tf_root_module_id": top.get("tf_root_module_id"),
            "tf_address": top.get("tf_address"),
            "tf_address_id": top.get("tf_address_id"),
            "module_path": top.get("module_path"),
            "tf_state_file_id": top.get("tf_state_file_id"),
            "state_backend": top.get("state_backend"),
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

    def _resource_ids_for_lookup(
        self,
        resource_id: str,
        nodes: Dict[str, ResourceNode],
    ) -> List[str]:
        if resource_id in nodes:
            return [resource_id]

        candidate = str(resource_id or "").strip()
        if not candidate:
            return []

        matches: List[str] = []
        seen = set()
        wildcard = "*" in candidate
        for node in nodes.values():
            if self._resource_node_matches(node, candidate, wildcard=wildcard):
                if node.resource_id not in seen:
                    seen.add(node.resource_id)
                    matches.append(node.resource_id)
        return matches

    @staticmethod
    def _resource_node_matches(
        node: ResourceNode,
        candidate: str,
        *,
        wildcard: bool,
    ) -> bool:
        if node.resource_id == candidate:
            return True

        if wildcard and fnmatch.fnmatch(node.resource_id, candidate):
            return True

        if node.name == candidate:
            return True

        if not candidate.startswith("arn:") and node.resource_id.startswith("arn:"):
            suffixes = (f"/{candidate}", f":{candidate}")
            if any(node.resource_id.endswith(suffix) for suffix in suffixes):
                return True

        return False

    def _collect_terraform_candidates(
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
            state_edges = [
                edge
                for edge in incoming.get(instance.resource_id, [])
                if edge.relationship_type == "contains"
            ]
            address_edges = [
                edge
                for edge in outgoing.get(instance.resource_id, [])
                if edge.relationship_type == "corresponds_to"
            ]
            for state_edge in state_edges:
                state_file = nodes.get(state_edge.from_resource_id)
                write_edges = [
                    edge
                    for edge in incoming.get(state_edge.from_resource_id, [])
                    if edge.relationship_type == "writes"
                ]
                roots = [nodes.get(edge.from_resource_id) for edge in write_edges]
                roots = [root for root in roots if root]
                if not roots:
                    candidates.append(
                        self._candidate_payload(
                            resource=resource,
                            instance=instance,
                            state_file=state_file,
                            tf_root=None,
                            tf_address=None,
                            github_file=None,
                            github_repo=None,
                            verified=False,
                            reasons=["exact state observation"],
                            missing_data_notes=["No Terraform root module writes the matched state file"],
                        )
                    )
                    continue

                for tf_root in roots:
                    matching_addresses = [
                        edge for edge in address_edges
                        if (edge.attributes or {}).get("tf_root_module_id") in (None, tf_root.resource_id)
                    ]
                    if not matching_addresses:
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
                                reasons=["exact state observation", "root module found"],
                                missing_data_notes=["No scoped Terraform address match found for the state instance"],
                            )
                        )
                        continue

                    for address_edge in matching_addresses:
                        tf_address = nodes.get(address_edge.to_resource_id)
                        declares = [
                            edge
                            for edge in incoming.get(address_edge.to_resource_id, [])
                            if edge.relationship_type == "declares"
                        ]
                        if not declares:
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
                                    reasons=["exact state observation", "tf_address located"],
                                    missing_data_notes=["No Terraform file declares the matched tf_address"],
                                )
                            )
                            continue

                        for declare_edge in declares:
                            github_file = nodes.get(declare_edge.from_resource_id)
                            if not github_file:
                                continue
                            part_of_match = any(
                                edge.relationship_type == "part_of" and edge.to_resource_id == tf_root.resource_id
                                for edge in outgoing.get(github_file.resource_id, [])
                            )
                            repo_edges = [
                                edge
                                for edge in incoming.get(github_file.resource_id, [])
                                if edge.relationship_type == "contains"
                            ]
                            github_repo = nodes.get(repo_edges[0].from_resource_id) if repo_edges else None
                            reasons = [
                                "exact state observation",
                                "exact Terraform address match",
                                "declaring Terraform file located",
                            ]
                            if part_of_match:
                                reasons.append("declaring file belongs to matching root module")
                            verified = bool(part_of_match and github_repo and tf_address)
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
                                    missing_data_notes=[] if verified else ["Declaring file is not linked to the matched root module"],
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
        if state_file:
            score += 0.1
        if tf_root:
            score += 0.15
        if tf_address:
            score += 0.15
        if github_file:
            score += 0.05
        if github_repo:
            score += 0.05
        if verified:
            score += 0.05

        file_path = (github_file.attributes or {}).get("path") if github_file else None
        tf_root_path = (tf_root.attributes or {}).get("path") if tf_root else None
        tf_address_value = (
            (tf_address.attributes or {}).get("terraform_address")
            if tf_address else (instance.attributes or {}).get("resource_address")
        )
        state_backend = (state_file.attributes or {}).get("s3_uri") if state_file else None
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
        if tf_root:
            path.extend(
                [
                    {"edge_type": "part_of", "direction": "outgoing"},
                    {"node_id": tf_root.resource_id},
                ]
            )
        if github_repo:
            path.extend(
                [
                    {"edge_type": "contains", "direction": "incoming"},
                    {"node_id": github_repo.resource_id},
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
        if state_file:
            evidence.append(
                {
                    "type": "tf_state_file",
                    "source": state_file.source or "tf_state",
                    "value": state_file.resource_id,
                    "confidence": "high",
                }
            )
        if tf_address:
            evidence.append(
                {
                    "type": "tf_address",
                    "source": tf_address.source or "derived",
                    "value": tf_address.resource_id,
                    "confidence": "high" if verified else "medium",
                }
            )
        if github_file:
            evidence.append(
                {
                    "type": "github_file",
                    "source": github_file.source or "git_scan",
                    "value": file_path,
                    "confidence": "high" if verified else "medium",
                }
            )
        if tf_root:
            evidence.append(
                {
                    "type": "tf_root_module",
                    "source": tf_root.source or "git_scan",
                    "value": tf_root.resource_id,
                    "confidence": "high" if verified else "medium",
                }
            )

        return {
            "repo": github_repo.name if github_repo else None,
            "file": file_path,
            "tf_root_module": tf_root_path or (tf_root.name if tf_root else None),
            "tf_root_module_id": tf_root.resource_id if tf_root else None,
            "tf_address": tf_address_value,
            "tf_address_id": tf_address.resource_id if tf_address else None,
            "module_path": (instance.attributes or {}).get("module_path"),
            "tf_state_file_id": state_file.resource_id if state_file else None,
            "state_backend": state_backend,
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
        }
