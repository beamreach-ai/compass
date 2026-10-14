from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from compass_core.events import emit_job_event
from compass_core.tables import tables
from infra_map.store import InfraMapStore

logger = logging.getLogger("iac-fix-drift-job")

_TFROOT_GITHUB_RE = re.compile(r"tfroot://github/([^/]+/[^/]+)/(.+)")


def _repo_from_tf_root_id(tf_root_id: str) -> Optional[str]:
    m = _TFROOT_GITHUB_RE.match(str(tf_root_id or ""))
    return m.group(1) if m else None


class IACFixDriftJob:
    def __init__(self, org_id: str, target: str, config: Dict[str, Any]):
        self.org_id = org_id
        self.target = target
        self.config = config

    def run(self, *, resource_ids: List[str]) -> Dict[str, Any]:
        emit_job_event("Loading infra map for drift resolution", event_type="progress", status="running")
        store = self._load_store()

        nodes = store.batch_get_resources(self.org_id, resource_ids)

        emit_job_event("Resolving resource ownership", event_type="progress", status="running")
        ownership = self._batch_lookup_ownership(store, resource_ids)

        by_repo: Dict[str, List[Dict]] = defaultdict(list)
        skipped: List[str] = []
        for rid in resource_ids:
            node = nodes.get(rid)
            owner = ownership.get(rid) or {}
            repo = owner.get("repo")
            if not node:
                skipped.append(rid)
                continue
            if not repo:
                logger.warning("No repo found for drifted resource %s", rid)
                skipped.append(rid)
                continue
            by_repo[repo].append({"node": node, "ownership": owner})

        if skipped:
            logger.info("Skipped %d resources with no ownership info", len(skipped))

        pr_results: List[Dict[str, str]] = []
        for repo, items in by_repo.items():
            emit_job_event(f"Creating drift fix PR for {repo}", event_type="progress", status="running")
            try:
                pr_url = self._fix_drift_in_repo(repo, items)
                if pr_url:
                    pr_results.append({"repo": repo, "pr_url": pr_url})
            except Exception as exc:
                logger.error("Failed to create drift fix PR for %s: %s", repo, exc)

        return {
            "pr_urls": [r["pr_url"] for r in pr_results],
            "repos": [r["repo"] for r in pr_results],
            "resource_count": len(resource_ids) - len(skipped),
            "skipped_count": len(skipped),
        }

    def _load_store(self) -> InfraMapStore:
        table_name = tables(self.config).map
        if not table_name:
            raise RuntimeError("dynamodb_config.map is not configured")
        workspace_id = self.config.get("workspace_id") or self.target
        return InfraMapStore(table_name=table_name, workspace_id=workspace_id)

    def _batch_lookup_ownership(
        self,
        store: InfraMapStore,
        resource_ids: List[str],
    ) -> Dict[str, Dict]:
        graph = store.fetch_graph(self.org_id)
        all_nodes = {n.resource_id: n for n in graph.get("nodes", [])}

        incoming: Dict[str, List] = defaultdict(list)
        for edge in graph.get("edges", []):
            incoming[edge.to_resource_id].append(edge)

        result: Dict[str, Dict] = {}
        for rid in resource_ids:
            obs_edges = [e for e in incoming.get(rid, []) if e.relationship_type == "observes"]
            if not obs_edges:
                continue
            instance = all_nodes.get(obs_edges[0].from_resource_id)
            if not instance:
                continue

            state_file = None
            for e in incoming.get(instance.resource_id, []):
                if e.relationship_type == "contains":
                    state_file = all_nodes.get(e.from_resource_id)
                    break

            tf_root = None
            if state_file:
                for e in incoming.get(state_file.resource_id, []):
                    if e.relationship_type == "writes":
                        tf_root = all_nodes.get(e.from_resource_id)
                        break

            if not tf_root:
                continue

            tf_root_attrs = tf_root.attributes or {}
            repo = tf_root_attrs.get("repo") or _repo_from_tf_root_id(tf_root.resource_id)
            module_path = tf_root_attrs.get("path")
            instance_attrs = instance.attributes or {}

            result[rid] = {
                "repo": repo,
                "module_path": module_path,
                "resource_address": instance_attrs.get("resource_address"),
                "tf_root_id": tf_root.resource_id,
            }

        return result

    def _fix_drift_in_repo(self, repo: str, items: List[Dict]) -> str:
        from captains.iac.github_pr_writer import GitHubPRWriter
        from captains.iac.hcl_generator import HCLGenerator

        hcl_gen = HCLGenerator(target=self.target, config=self.config)

        drift_data = []
        for item in items:
            node = item["node"]
            attrs = node.attributes or {}
            drift_fields = attrs.get("drift_fields") or []
            owner = item.get("ownership") or {}
            drift_data.append({
                "resource_id": node.resource_id,
                "resource_type": node.type,
                "name": node.name,
                "region": node.region,
                "drift_fields": drift_fields,
                "resource_address": owner.get("resource_address"),
                "module_path": owner.get("module_path"),
            })

        hcl_content = hcl_gen.generate_drift_fix(drift_data)
        resources = [item["node"] for item in items]

        writer = GitHubPRWriter(config=self.config)
        return writer.create_drift_fix_pr(
            repo=repo,
            base_branch="main",
            hcl_content=hcl_content,
            drift_data=drift_data,
            resources=resources,
        )
