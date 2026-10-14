from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from compass_core.aws import resolve_target_region
from compass_core.events import emit_job_event
from compass_core.tables import tables
from infra_map.models import ResourceNode
from infra_map.store import InfraMapStore

logger = logging.getLogger("iac-import-job")


class IACImportJob:
    def __init__(self, org_id: str, target: str, config: Dict[str, Any]):
        self.org_id = org_id
        self.target = target
        self.config = config

    def run(
        self,
        *,
        resource_ids: List[str],
        repo: str,
        branch: str,
        module_path: Optional[str] = None,
        is_new_module: bool = False,
    ) -> Dict[str, Any]:
        emit_job_event("Fetching resource details from infra map", event_type="progress", status="running")
        resources = self._fetch_resources(resource_ids)
        if not resources:
            raise RuntimeError(f"No resources found for the given IDs: {resource_ids[:3]}")

        from captains.iac.hcl_generator import HCLGenerator

        hcl_gen = HCLGenerator(target=self.target, config=self.config)
        importable, skipped = hcl_gen.partition_importable(resources)
        if not importable:
            raise RuntimeError(
                "None of the selected resources can be imported into Terraform: "
                + "; ".join(f"{n.name} ({n.type}) — {why}" for n, why in skipped)
            )

        from captains.iac.tf_runner import TerraformRunner

        runner = TerraformRunner()
        if runner.available():
            return self._run_with_terraform(
                runner=runner,
                hcl_gen=hcl_gen,
                importable=importable,
                skipped=skipped,
                repo=repo,
                base_branch=branch,
                module_path=module_path or "",
            )

        logger.warning("terraform not available; falling back to generated-HCL import")
        emit_job_event(
            "Terraform unavailable — generating configuration without verification",
            event_type="progress",
            status="running",
        )
        return self._run_legacy(
            hcl_gen=hcl_gen,
            resources=importable,
            repo=repo,
            branch=branch,
            module_path=module_path,
            is_new_module=is_new_module,
        )

    # ------------------------------------------------------------- pipelines

    def _run_with_terraform(
        self,
        *,
        runner: Any,
        hcl_gen: Any,
        importable: List[ResourceNode],
        skipped: List[Any],
        repo: str,
        base_branch: str,
        module_path: str,
    ) -> Dict[str, Any]:
        """The real path: terraform generates, the index rewires, the plan verifies."""
        from captains.iac.github_pr_writer import _IAC_BRANCH_PREFIX, GitHubPRWriter, _content_branch_suffix
        from captains.iac.import_pipeline import ImportPipeline
        from captains.iac.pr_body import import_pr_body
        from compass_core.aws import target_credentials_env
        from compass_core.github import resolve_github_token

        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required for creating PRs")

        # Terraform generates configuration by reading the live resources, which
        # live in the *target* account. The worker's own identity is the
        # control-plane account, from which those resources are invisible — so
        # without these credentials generation fails with "Cannot import
        # non-existent remote object" for every resource.
        # Global services (S3, IAM, Route 53) carry region "global" in the map; the
        # provider needs a real one, so prefer a regional resource's, then the workspace's.
        region = next((r.region for r in importable if r.region and r.region != "global"), None) or resolve_target_region(self.config) or "us-east-1"
        target_credentials = target_credentials_env(self.config, capability="read", region=region)
        if not target_credentials:
            logger.warning(
                "no target-account credentials resolved; terraform will authenticate as the worker "
                "and configuration generation is expected to fail"
            )

        import_blocks = hcl_gen.generate_import_blocks(importable)
        addresses = hcl_gen.resource_addresses(importable)
        import_ids = hcl_gen.import_ids(importable)
        branch_name = f"{_IAC_BRANCH_PREFIX}{_content_branch_suffix(importable)}"

        emit_job_event(
            f"Generating Terraform configuration for {len(importable)} resource(s)",
            event_type="progress",
            status="running",
        )
        pipeline = ImportPipeline(
            token=token,
            terraform=runner,
            region=region,
            target_credentials=target_credentials,
        )
        result = pipeline.run(
            repo=repo,
            base_branch=base_branch,
            branch=branch_name,
            module_path=module_path,
            import_blocks=import_blocks,
            resources=importable,
            addresses=addresses,
            import_ids=import_ids,
            public_repo=True,
            commit_message=f"beamreach: import {len(importable)} unmanaged resource(s)",
        )

        emit_job_event(
            "Plan verified — import only" if result.is_clean else "Plan needs review",
            event_type="progress",
            status="running",
        )

        writer = GitHubPRWriter(config=self.config)
        names = ", ".join(r.name for r in importable[:5])
        if len(importable) > 5:
            names += f" and {len(importable) - 5} more"
        prefix = "" if result.is_clean else "[needs review] "

        title = f"{prefix}IAC Import: {names}"
        pr_url = writer.create_or_update_pr(
            repo=repo,
            branch=branch_name,
            base_branch=base_branch,
            title=title,
            body=import_pr_body(importable, skipped, result),
            gh_repo=None,
        )
        self._register_pr(
            pr_url=pr_url, branch=branch_name, base_branch=base_branch, title=title,
            addresses=addresses, resource_ids=[r.resource_id for r in importable], files=list(result.files.keys()),
            verified=result.verified, plan_clean=result.is_clean,
        )

        return {
            "pr_url": pr_url,
            "resource_count": len(importable),
            "skipped_count": len(skipped),
            "files": list(result.files.keys()),
            "verified": result.verified,
            "plan_clean": result.is_clean,
            "rewired": result.rewired.count if result.rewired else 0,
            "redacted": len(result.redacted),
            "warnings": result.warnings,
        }

    def _register_pr(self, *, pr_url: str, branch: str, base_branch: str, title: str, addresses: List[str], resource_ids: List[str], files: List[str], verified: bool, plan_clean: bool) -> None:
        """Register the PR with its intent so the watcher can keep it alive (docs/pr_watcher_spec.md §2)."""
        try:
            from captains.prs.intent import intent_for_iac_import
            from compass_core.events import current_job_id
            from compass_core.registry import PRRegistry

            PRRegistry.from_config(self.config).record(
                url=pr_url,
                pr_type="iac_import",
                origin={"captain": "iac", "command": "import", "job_id": current_job_id(), "resource_ids": list(resource_ids)},
                intent=intent_for_iac_import(addresses=addresses, resource_ids=resource_ids, files=files, verified=verified, plan_clean=plan_clean),
                branch=branch,
                base_branch=base_branch,
                title=title,
            )
        except Exception as exc:
            logger.warning("PR registry write failed: %s", exc)

    def _run_legacy(
        self,
        *,
        hcl_gen: Any,
        resources: List[ResourceNode],
        repo: str,
        branch: str,
        module_path: Optional[str],
        is_new_module: bool,
    ) -> Dict[str, Any]:
        """Fallback for environments without terraform.

        Produces configuration that is safe — it never touches an existing file
        — but unverified and, where the map lacks attributes, incomplete.
        """
        from captains.iac.github_pr_writer import GitHubPRWriter

        hcl_files = hcl_gen.generate(resources, module_path=module_path, is_new_module=is_new_module)

        emit_job_event("Creating GitHub pull request", event_type="progress", status="running")
        writer = GitHubPRWriter(config=self.config)
        pr_url = writer.create_pr(
            repo=repo,
            base_branch=branch,
            hcl_files=hcl_files,
            resources=resources,
        )

        return {
            "pr_url": pr_url,
            "resource_count": len(resources),
            "files": list(hcl_files.keys()),
            "verified": False,
            "plan_clean": False,
        }

    # ---------------------------------------------------------------- lookup

    def _fetch_resources(self, resource_ids: List[str]) -> List[ResourceNode]:
        table_name = tables(self.config).map
        if not table_name:
            raise RuntimeError("dynamodb_config.map is not configured")
        workspace_id = self.config.get("workspace_id") or self.target
        store = InfraMapStore(table_name=table_name, workspace_id=workspace_id)
        node_map = store.batch_get_resources(self.org_id, resource_ids)
        resources = [node_map[rid] for rid in resource_ids if rid in node_map]
        logger.info("Fetched %d of %d requested resources", len(resources), len(resource_ids))
        return resources
