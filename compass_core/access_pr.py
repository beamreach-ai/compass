"""The pull request that grants or revokes access: the access module regenerated whole
(docs/specs/access-model.md §9.2). Opened by the GitHub identity Compass already uses, on
the IaC repository, under the ``beamreach/`` branch prefix; the PR watcher tracks it, and
the grant tick reads its state back.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from compass_core import access_module
from compass_core.github import client as github_client
from compass_core.github import resolve_github_token
from compass_core.github_prs import commit_files, ensure_branch, iac_repository, iac_terraform_root, open_or_update_pr
from compass_core.grants import GRANTED, is_granted, list_grants, statements_for
from compass_core.keys import workspace_pk
from compass_core.registry import PRRegistry
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("compass.access_pr")

BRANCH = "beamreach/access"
PR_TYPE = "access"


repository = iac_repository
terraform_root = iac_terraform_root


def _path(root: str, rel: str) -> str:
    return f"{root}/{rel}" if root else rel


def describe(row: Dict[str, Any], config: Dict[str, Any]) -> str:
    from compass_core import actions as _actions

    entry = _actions.describe_action(str(row.get("action_type") or ""))
    scope = ", ".join(f"{k}: {', '.join(map(str, v))}" for k, v in (row.get("scope") or {}).items() if v) or "no scope"
    statements = statements_for(row, config)
    actions = sorted({a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])})
    who = row.get("accepted_by") or row.get("removed_by") or row.get("created_by") or "a person"
    verb = "grants" if is_granted(row) else "revokes"
    return (
        f"- **{entry.get('title') or row.get('action_type')}** ({row.get('action_type')}, {entry.get('change_type') or 'change'}"
        f"{', rollback' if entry.get('rollback_supported') else ''}): {verb} `{'`, `'.join(actions)}` on {scope}; "
        f"mode `{row.get('mode')}`, risk cap {row.get('risk_max')}; {'accepted' if is_granted(row) else 'removed'} by {who}."
    )


def open_pr(config: Dict[str, Any], *, workspace_id: str, actor: Optional[str] = None, reason: str = "", table: Any = None,
            with_imports: Optional[bool] = None) -> Dict[str, Any]:
    """Render the module as it should be now and open (or update) the one pull request for it.
    Every grant in ``pr_open`` / ``revoke_pr_open`` records its URL."""
    repo_name = repository(config)
    if not repo_name:
        raise RuntimeError("no IaC repository configured (github.iac_repo): connect a repository to enable write actions")
    token = resolve_github_token(config, capability="write")
    if not token:
        raise RuntimeError("GitHub write token is required")
    table = table or dynamo_table(tables(config).items)
    pk = workspace_pk(workspace_id)
    grants = list_grants(table, pk)
    gh_repo = github_client(token).get_repo(repo_name)
    github_cfg = config.get("github") if isinstance(config.get("github"), dict) else {}
    base_branch = str(github_cfg.get("default_branch") or github_cfg.get("base_branch") or "main")
    root = terraform_root(config)
    ensure_branch(gh_repo, BRANCH, base_branch)
    if with_imports is None:
        try:
            gh_repo.get_contents(_path(root, f"{access_module.MODULE_DIR}/main.tf"), ref=base_branch)
            with_imports = False
        except Exception:  # noqa: BLE001 - not there yet: the first pull request imports the roles
            with_imports = True
    rendered = access_module.render(config, grants, with_imports=with_imports)
    files = {_path(root, path): text for path, text in rendered["files"].items()}
    files[_path(root, rendered["root_file"])] = rendered["root"]
    commit_files(gh_repo, BRANCH, files, add_message="beamreach: compass access module", update_message="beamreach: compass access module")
    carried = [r for r in grants if is_granted(r)]
    leaving = [r for r in grants if str(r.get("grant_status")) in ("disarmed", "revoke_pr_open", "revoke_merged")]
    lines: List[str] = []
    if carried:
        lines.append("The act role may:\n" + "\n".join(describe(r, config) for r in carried))
    if leaving:
        lines.append("Removed from the act role:\n" + "\n".join(describe(r, config) for r in leaving))
    if not carried and not leaving:
        lines.append("The act role has no permissions; the read role's policy follows the installed captains' manifests.")
    body = (
        "Compass's access to this account, as it should be now: the read role (its policy generated from the installed captains' "
        "manifests) and the act role (the union of the actions a person enabled). Generated whole on every change; nothing here is "
        "hand-edited.\n\n" + "\n\n".join(lines) + "\n\nAfter the apply, Compass reads the roles back and arms the actions whose "
        "statements IAM holds and allows. " + (f"Reason: {reason}." if reason else "")
    )
    published = open_or_update_pr(gh_repo, branch=BRANCH, base_branch=base_branch, title="Compass access: the roles as they should be now", body=body)
    for row in grants:
        if str(row.get("grant_status")) in ("pr_open", "disarmed"):
            row["grant_pr"] = published.url
            if str(row.get("grant_status")) == "disarmed":
                from compass_core.grants import _put, _transition

                _transition(row, "revoke_pr_open", actor=actor or "pr")
                _put(table, row)
            else:
                from compass_core.grants import _put

                _put(table, row)
    try:
        PRRegistry.from_config(config, workspace_id=workspace_id).record(
            url=published.url, pr_type=PR_TYPE, origin={"captain": "core", "command": "access", "workspace_id": workspace_id, "requested_by": actor or ""},
            intent={"grants": [r["action_id"] for r in grants if str(r.get("grant_status")) in GRANTED + ("revoke_pr_open",)], "reason": reason},
            branch=BRANCH, base_branch=base_branch, title="Compass access: the roles as they should be now",
        )
    except Exception as exc:  # noqa: BLE001 - the PR exists; the watcher picks it up next sync
        logger.warning("access pull request not registered with the watcher: %s", exc)
    logger.info("access pull request: %s", published.url)
    return {"pr_url": published.url, "branch": BRANCH, "repo": repo_name, "grants": len(carried), "revocations": len(leaving), "imports": with_imports}
