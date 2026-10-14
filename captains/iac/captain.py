from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from compass_core.events import emit_job_event

logger = logging.getLogger("iac-captain")


class IACCaptain:
    def __init__(
        self,
        org_id: str,
        target: str,
        config: Dict[str, Any],
        workspace_ctx: Optional[Any] = None,
    ):
        self.org_id = org_id
        self.target = target
        self.config = config or {}
        self.workspace_ctx = workspace_ctx

    def run_import(
        self,
        *,
        resource_ids: List[str],
        repo: str,
        branch: str,
        module_path: Optional[str] = None,
        is_new_module: bool = False,
    ) -> Dict[str, Any]:
        from captains.iac.import_job import IACImportJob

        emit_job_event("Starting Terraform import job", event_type="progress", status="running")
        job = IACImportJob(org_id=self.org_id, target=self.target, config=self.config)
        result = job.run(
            resource_ids=resource_ids,
            repo=repo,
            branch=branch,
            module_path=module_path,
            is_new_module=is_new_module,
        )
        pr_url = result.get("pr_url", "")
        emit_job_event(
            f"Terraform import PR created: {pr_url}" if pr_url else "Terraform import completed",
            event_type="progress",
            status="completed",
            data=result,
        )
        return result

    def run_fix_drift(self, *, resource_ids: List[str]) -> Dict[str, Any]:
        from captains.iac.fix_drift_job import IACFixDriftJob

        emit_job_event("Starting drift fix job", event_type="progress", status="running")
        job = IACFixDriftJob(org_id=self.org_id, target=self.target, config=self.config)
        result = job.run(resource_ids=resource_ids)
        count = result.get("resource_count", 0)
        emit_job_event(
            f"Drift fix completed for {count} resource(s)",
            event_type="progress",
            status="completed",
            data=result,
        )
        return result
