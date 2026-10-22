from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional

from compass_core import sessions
from compass_core.github import client as github_client
from compass_core.github import resolve_github_token
from compass_core.github_prs import PublishedPR, commit_files, ensure_branch, find_open_pr, open_or_update_pr
from infra_map.models import ResourceNode

logger = logging.getLogger("iac-github-pr")

_IAC_BRANCH_PREFIX = "beamreach/iac-import/"


def _slug(value: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9\-]", "-", (value or "").lower()).strip("-")[:limit].strip("-")


def _content_branch_suffix(resources: List[ResourceNode]) -> str:
    """A branch name derived from the full resource set, not a truncated prefix.

    The previous scheme joined the first three resource names and cut the result
    to 50 characters, which made the branch a function of only part of the
    selection: two different imports whose leading names shared a long prefix
    silently landed on the same branch. Hashing every resource id removes that
    aliasing, while the leading name keeps the branch recognisable to a human
    scanning the branch list.
    """
    if not resources:
        return "import"
    # Sort first, then derive *both* halves from that order. The dashboard
    # builds its selection from a Set, so the order resources arrive in is not
    # stable between runs — taking the readable prefix from resources[0] would
    # produce a different branch for an identical selection and defeat the
    # create-or-update path below.
    ordered = sorted(resources, key=lambda r: r.resource_id or "")
    digest = hashlib.sha256("\n".join((r.resource_id or "") for r in ordered).encode("utf-8")).hexdigest()[:10]
    lead = _slug(ordered[0].name or "", 28)
    return f"{lead}-{digest}" if lead else digest


_find_open_pr = find_open_pr


class GitHubPRWriter:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.last_pr: Optional[PublishedPR] = None

    def create_pr(
        self,
        *,
        repo: str,
        base_branch: str,
        hcl_files: Dict[str, str],
        resources: List[ResourceNode],
    ) -> str:
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required for creating PRs")

        g = github_client(token)
        gh_repo = g.get_repo(repo)

        suffix = _content_branch_suffix(resources)
        branch_name = f"{_IAC_BRANCH_PREFIX}{suffix}"

        ensure_branch(gh_repo, branch_name, base_branch)
        commit_files(
            gh_repo,
            branch_name,
            hcl_files,
            add_message="beamreach: add {path} for IAC import",
            update_message="beamreach: update {path} for IAC import",
        )

        resource_names = ", ".join(r.name for r in resources[:5])
        if len(resources) > 5:
            resource_names += f" and {len(resources) - 5} more"

        return self.create_or_update_pr(
            repo=repo,
            branch=branch_name,
            base_branch=base_branch,
            title=f"IAC Import: {resource_names}",
            body=self._pr_body(resources, hcl_files),
            gh_repo=gh_repo,
        )

    def create_or_update_pr(
        self,
        *,
        repo: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        gh_repo: Any = None,
    ) -> str:
        """Open a PR for an existing branch, or refresh the one already open.

        Separated from file writing so the git-based pipeline — which pushes its
        own commits from a real checkout — can publish without going through the
        Contents API at all.

        The branch is content-addressed, so re-importing the same selection
        lands on the same branch, and GitHub refuses a second pull request for a
        head that already has one open. Updating is the correct response: the
        branch already carries the refreshed change.
        """
        if gh_repo is None:
            token = resolve_github_token(self.config, capability="write")
            if not token:
                raise RuntimeError("GitHub write token is required for creating PRs")
            gh_repo = github_client(token).get_repo(repo)

        self.last_pr = open_or_update_pr(gh_repo, branch=branch, base_branch=base_branch, title=title, body=body)
        return self.last_pr.url

    def create_drift_fix_pr(
        self,
        *,
        repo: str,
        base_branch: str,
        hcl_content: str,
        drift_data: List[Dict[str, Any]],
        resources: List[ResourceNode],
    ) -> str:
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required for creating PRs")

        g = github_client(token)
        gh_repo = g.get_repo(repo)

        suffix = _content_branch_suffix(resources)
        branch_name = f"beamreach/drift-fix/{suffix}"

        ensure_branch(gh_repo, branch_name, base_branch)

        # Determine file path: use the module subdirectory if all resources share one
        module_paths = list({(d.get("module_path") or "").replace(".", "/").strip("/") for d in drift_data})
        if len(module_paths) == 1 and module_paths[0]:
            file_path = f"{module_paths[0]}/drift_fix.tf"
        else:
            file_path = "drift_fix.tf"

        message = f"beamreach: drift fix for {len(resources)} resource(s)"
        commit_files(gh_repo, branch_name, {file_path: hcl_content}, add_message=message, update_message=message)

        resource_names = ", ".join(r.name for r in resources[:3])
        if len(resources) > 3:
            resource_names += f" +{len(resources) - 3} more"

        title = f"Drift Fix: {resource_names}"
        body = self._drift_fix_pr_body(drift_data, resources, file_path)

        self.last_pr = open_or_update_pr(gh_repo, branch=branch_name, base_branch=base_branch, title=title, body=body)
        pr_url = self.last_pr.url
        self._register_drift_pr(pr_url=pr_url, branch=branch_name, base_branch=base_branch, title=title, drift_data=drift_data, files=[file_path])
        return pr_url

    def _register_drift_pr(self, *, pr_url: str, branch: str, base_branch: str, title: str, drift_data: List[Dict[str, Any]], files: List[str]) -> None:
        """Register the PR with its intent so the watcher can keep it alive (docs/pr_watcher_spec.md §2)."""
        try:
            from captains.prs.intent import intent_for_fix_drift
            from compass_core.events import current_job_id
            from compass_core.registry import PRRegistry

            PRRegistry.from_config(self.config).record(
                url=pr_url,
                pr_type="fix_drift",
                origin={"captain": "iac", "command": "fix_drift", "job_id": current_job_id(), "resource_ids": [str(d.get("resource_id")) for d in drift_data if d.get("resource_id")],
                        "user_id": sessions.current_requester()},
                intent=intent_for_fix_drift(drift_data=drift_data, files=files),
                branch=branch,
                base_branch=base_branch,
                title=title,
            )
        except Exception as exc:
            logger.warning("PR registry write failed: %s", exc)

    def _drift_fix_pr_body(
        self,
        drift_data: List[Dict[str, Any]],
        resources: List[ResourceNode],
        file_path: str,
    ) -> str:
        lines = [
            "## Drift Fix",
            "",
            "This PR was generated by **Beamreach Compass** to correct configuration drift between declared Terraform state and live AWS resources.",
            "",
            "### Affected Resources",
            "",
        ]
        for d in drift_data:
            lines.append(f"- `{d.get('name', d['resource_id'])}` (`{d['resource_id']}`)")
            for f in d.get("drift_fields", []):
                lines.append(f"  - `{f['field']}`: declared `{f['declared']}` → actual `{f['actual']}`")
        lines += [
            "",
            "### File",
            "",
            f"- `{file_path}`",
            "",
            "### Next Steps",
            "",
            "1. Review the generated resource blocks",
            "2. Move the corrected blocks into the appropriate module file",
            "3. Remove the temporary `drift_fix.tf` file",
            "4. Run `terraform plan` to confirm no remaining drift",
            "",
            "---",
            "*Generated by [Compass](https://github.com/beamreach-ai/compass)*",
        ]
        return "\n".join(lines)

    def _pr_body(self, resources: List[ResourceNode], hcl_files: Dict[str, str]) -> str:
        lines = [
            "## Terraform Import",
            "",
            "This PR was generated by **Beamreach Compass** to bring unmanaged AWS resources under Terraform control.",
            "",
            "### Resources",
            "",
        ]
        for r in resources:
            lines.append(f"- `{r.type}/{r.name}` (`{r.resource_id}`)")
        lines += [
            "",
            "### Files changed",
            "",
        ]
        for f in hcl_files:
            lines.append(f"- `{f}`")
        lines += [
            "",
            "### Next steps",
            "",
            "1. Review the generated resource blocks and adjust as needed",
            "2. Run `terraform init && terraform plan` to verify the import plan",
            "3. Run `terraform apply` to complete the import",
            "",
            "---",
            "*Generated by [Compass](https://github.com/beamreach-ai/compass)*",
        ]
        return "\n".join(lines)
