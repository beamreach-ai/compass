"""Creates GitHub PRs for honeytoken plant/decommission operations."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import hcl2
from github import GithubException

from captains.honeypot.honeytoken_material import default_cover_name
from captains.honeypot.task_env import TASK_DEF_TYPE, find_task_definition, inject_env, remove_env
from captains.honeypot.terraform_generator import (
    ACCOUNT_MODULE_DIR,
    ACCOUNT_MODULE_NAME,
    ROOT_FILE,
    SERVICES_DIR,
    TOKEN_MODULE_PREFIX,
    account_module_files,
)
from compass_core.github import client as github_client
from compass_core.github import resolve_github_token
from compass_core.github_prs import PublishedPR, commit_files, ensure_branch, open_or_update_pr

logger = logging.getLogger("honeypot-pr-writer")

_BRANCH_PREFIX = "beamreach/honeypot/"


def _safe_branch_suffix(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "-", name.lower())[:50].strip("-") or "token"


# ── services.tf: the root-level file that loads the generated modules ─────
#
# The file is owned by Compass (customer files are never edited). hcl2 can parse
# but not serialise, so the file is modelled as {modules, outputs}, mutated, and
# re-rendered from a deterministic template: svc_account first, then tokens and
# outputs sorted by name. Re-planting a token therefore updates in place.

HONEYTOKENS_FILE = ROOT_FILE
_TOKEN_MODULE_PREFIX = TOKEN_MODULE_PREFIX


def _unq(value: str) -> str:
    """The string inside a quoted HCL expression; a reference or a bare value unchanged."""
    text = str(value or "").strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


class HoneytokensFile:
    def __init__(self) -> None:
        # module name -> {attribute: raw HCL expression text}
        self.modules: Dict[str, Dict[str, str]] = {}
        # output name -> raw HCL expression text of its `value`
        self.outputs: Dict[str, str] = {}

    @property
    def has_account_module(self) -> bool:
        return ACCOUNT_MODULE_NAME in self.modules

    def upsert_account(self, queue_arn: str) -> None:
        self.modules[ACCOUNT_MODULE_NAME] = {
            "source": _q(f"./{ACCOUNT_MODULE_DIR}"),
            "alert_queue_arn": _q(queue_arn),
        }

    def upsert_token(
        self,
        slug: str,
        *,
        queue_arn: str,
        workspace_id: str,
        outputs: Dict[str, str],
        extra_inputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """``extra_inputs`` are module inputs beyond the shared four (e.g. the
        network a decoy service runs in); strings are quoted, lists rendered."""
        name = f"{_TOKEN_MODULE_PREFIX}{slug}"
        self.modules[name] = {
            "source": _q(f"./{SERVICES_DIR}/{slug}"),
            "workspace_id": _q(workspace_id),
            "alert_queue_arn": _q(queue_arn),
            "alert_delivery_role_arn": f"module.{ACCOUNT_MODULE_NAME}.alert_delivery_role_arn",
        }
        for key, value in (extra_inputs or {}).items():
            if value is None or value == "" or value == []:
                continue
            if isinstance(value, (list, tuple)):
                self.modules[name][key] = "[" + ", ".join(_q(str(v)) for v in value) + "]"
            elif isinstance(value, bool):
                self.modules[name][key] = "true" if value else "false"
            elif isinstance(value, (int, float)):
                self.modules[name][key] = str(value)
            else:
                self.modules[name][key] = _q(str(value))
        self.remove_outputs(name)
        for module_output, suffix in outputs.items():
            self.outputs[f"{name}_{suffix}"] = f"module.{name}.{module_output}"

    def upsert_module(self, name: str, source_dir: str, attrs: Optional[Dict[str, str]] = None, outputs: Optional[Dict[str, str]] = None) -> None:
        """A root-level module block that is not a token (e.g. the responder role)."""
        self.modules[name] = {"source": _q(f"./{source_dir}"), **(attrs or {})}
        self.remove_outputs(name)
        for module_output, suffix in (outputs or {}).items():
            self.outputs[f"{name}_{suffix}"] = f"module.{name}.{module_output}"

    def remove_outputs(self, module_name: str) -> None:
        ref = f"module.{module_name}."
        for out in [o for o, expr in self.outputs.items() if expr.startswith(ref)]:
            del self.outputs[out]

    def remove_token(self, slug: str) -> bool:
        """Drop the token's module block and its outputs. Never touches svc_account."""
        name = f"{_TOKEN_MODULE_PREFIX}{slug}"
        if name == ACCOUNT_MODULE_NAME or name not in self.modules:
            return False
        del self.modules[name]
        self.remove_outputs(name)
        return True


def _q(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote_key(key: str) -> str:
    return key[1:-1] if len(key) >= 2 and key[0] == key[-1] == '"' else key


def _expr(value: Any) -> str:
    """hcl2 gives string literals back quoted, expressions as ${...}, and
    lists as Python lists of either; render them back as HCL."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_expr(v) for v in value) + "]"
    text = str(value)
    if text.startswith("${") and text.endswith("}"):
        return text[2:-1]
    return text


def parse_honeytokens_tf(text: str) -> HoneytokensFile:
    parsed = hcl2.loads(text or "")
    unexpected = set(parsed) - {"module", "output", "__comments__"}
    if unexpected:
        raise ValueError(
            f"{HONEYTOKENS_FILE} contains blocks Compass does not manage: {sorted(unexpected)}"
        )
    out = HoneytokensFile()
    for block in parsed.get("module", []):
        for raw_name, body in block.items():
            out.modules[_unquote_key(raw_name)] = {
                k: _expr(v) for k, v in body.items() if k != "__is_block__"
            }
    for block in parsed.get("output", []):
        for raw_name, body in block.items():
            out.outputs[_unquote_key(raw_name)] = _expr(body.get("value", ""))
    return out


def render_honeytokens_tf(doc: HoneytokensFile) -> str:
    names = sorted(doc.modules, key=lambda n: (n != ACCOUNT_MODULE_NAME, n))
    parts = ["# Service modules. Generated; manual edits may be overwritten.\n"]
    for name in names:
        attrs = doc.modules[name]
        width = max(len(k) for k in attrs)
        body = "".join(f"  {k.ljust(width)} = {v}\n" for k, v in attrs.items())
        parts.append(f'module "{name}" {{\n{body}}}\n')
    for out_name in sorted(doc.outputs):
        parts.append(f'output "{out_name}" {{\n  value = {doc.outputs[out_name]}\n}}\n')
    return "\n".join(parts)


class HoneypotPRWriter:
    def __init__(self, config: Dict[str, Any]):
        self.last_pr: Optional[PublishedPR] = None
        self.config = config

    def _get_repo_from_config(self) -> str:
        """Fallback repo from workspace config when infra map resolution fails."""
        github_cfg = self.config.get("github") or {}
        repo = github_cfg.get("honeypot_repo") or github_cfg.get("iac_repo")
        if not repo:
            raise RuntimeError(
                "Could not resolve a target repo from the infra map and no "
                "honeypot_repo or iac_repo is configured in GitHub settings."
            )
        return str(repo)

    def _get_base_branch(self) -> str:
        github_cfg = self.config.get("github") or {}
        return str(github_cfg.get("default_branch") or github_cfg.get("base_branch") or "main")

    def _resolve_repo(self, resolved_repo: Optional[str]) -> str:
        return resolved_repo or self._get_repo_from_config()

    def _prefix_files_with_module(
        self,
        hcl_files: Dict[str, str],
        module_path: Optional[str],
    ) -> Dict[str, str]:
        """If a module_path was resolved from the infra map, nest the honeypot
        files under it so they land alongside the managed resource's Terraform."""
        if not module_path:
            return hcl_files
        prefix = module_path.rstrip("/")
        return {f"{prefix}/{path}": content for path, content in hcl_files.items()}

    @staticmethod
    def _root_path(module_path: Optional[str], rel: str) -> str:
        prefix = (module_path or "").rstrip("/")
        return f"{prefix}/{rel}" if prefix else rel

    @staticmethod
    def _read_contents(gh_repo: Any, path: str, ref: str) -> Any:
        try:
            return gh_repo.get_contents(path, ref=ref)
        except GithubException as exc:
            if exc.status == 404:
                return None
            raise

    def _read_text(self, gh_repo: Any, path: str, ref: str) -> Optional[str]:
        item = self._read_contents(gh_repo, path, ref)
        return None if item is None else item.decoded_content.decode("utf-8")

    def _list_files(self, gh_repo: Any, directory: str, ref: str) -> List[str]:
        """Every file under `directory` (recursive); empty when it does not exist."""
        listing = self._read_contents(gh_repo, directory, ref)
        if listing is None:
            return []
        items = listing if isinstance(listing, list) else [listing]
        out: List[str] = []
        for item in items:
            if getattr(item, "type", "file") == "dir":
                out.extend(self._list_files(gh_repo, item.path, ref))
            else:
                out.append(item.path)
        return out

    def _root_tf_files(self, gh_repo: Any, module_path: Optional[str], ref: str) -> Dict[str, str]:
        """The customer's own .tf files at the Terraform root (not services.tf, not services/)."""
        root = (module_path or "").rstrip("/")
        listing = self._read_contents(gh_repo, root, ref) if root else self._read_contents(gh_repo, "", ref)
        items = listing if isinstance(listing, list) else ([listing] if listing is not None else [])
        out: Dict[str, str] = {}
        for item in items:
            path = getattr(item, "path", "")
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if (
                getattr(item, "type", "file") != "file"
                or parent != root
                or not path.endswith(".tf")
                or path.rsplit("/", 1)[-1] == HONEYTOKENS_FILE
            ):
                continue
            text = self._read_text(gh_repo, path, ref)
            if text is not None:
                out[path] = text
        return out

    def _task_env_edit(
        self, gh_repo: Any, module_path: Optional[str], ref: str, task_env: Dict[str, Any], *, remove: bool = False
    ) -> Optional[Dict[str, str]]:
        """{path, text, tf_address} after adding (or removing) the token's variables in the
        customer's task definition; None when nothing changes."""
        files = self._root_tf_files(gh_repo, module_path, ref)
        path, resource = find_task_definition(files, container=task_env["container"], tf_address=task_env.get("tf_address"))
        before = files[path]
        if remove:
            after = remove_env(before, resource=resource, container=task_env["container"], names=task_env["names"])
        else:
            after = inject_env(before, resource=resource, container=task_env["container"], entries=task_env["entries"])
        if after == before:
            return None
        return {"path": path, "text": after, "tf_address": f"{TASK_DEF_TYPE}.{resource}"}

    def create_plant_pr(
        self,
        *,
        honeytoken: Dict[str, Any],
        hcl_files: Dict[str, str],
        resolved_repo: Optional[str] = None,
        resolved_module_path: Optional[str] = None,
        module_slug: str,
        queue_arn: str,
        workspace_id: str,
        root_outputs: Optional[Dict[str, str]] = None,
        extra_inputs: Optional[Dict[str, Any]] = None,
        task_env: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Open a plant PR.

        `hcl_files` are the per-token module files (`services/<slug>/...`).
        Besides writing them, the PR (re)writes `<root>/services.tf` so the
        module is actually loaded, and creates the once-per-account `svc_account`
        module when the root does not have one yet. `root_outputs` maps module
        output name -> root output suffix; never pass a secret.

        `task_env` ({container, tf_address?, entries}) is an `ecs_task_env`
        placement: the PR also adds the token's variables to that container in
        the customer's task definition (HP-05). The edited address is left in
        `self.task_env_result`."""
        self.task_env_result: Optional[Dict[str, Any]] = None
        if not queue_arn:
            raise RuntimeError("Compass alert queue ARN is required to plant a honeytoken")
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required")

        g = github_client(token)
        repo_name = self._resolve_repo(resolved_repo)
        gh_repo = g.get_repo(repo_name)
        base_branch = self._get_base_branch()

        honeytoken_name = str(honeytoken.get("name") or default_cover_name(str(honeytoken.get("honeytoken_id", ""))))
        honeytoken_class = str(honeytoken.get("honeytoken_class", ""))
        branch_name = f"{_BRANCH_PREFIX}{_safe_branch_suffix(honeytoken_name)}"

        # Nest files under the resolved module path when available
        placed_files = self._prefix_files_with_module(hcl_files, resolved_module_path)

        ensure_branch(gh_repo, branch_name, base_branch)

        # Root file: parse what is already on the branch so a re-plant updates in place.
        root_file = self._root_path(resolved_module_path, HONEYTOKENS_FILE)
        existing_root = self._read_text(gh_repo, root_file, branch_name)
        doc = parse_honeytokens_tf(existing_root or "")
        if not doc.has_account_module:
            placed_files.update(
                self._prefix_files_with_module(account_module_files(), resolved_module_path)
            )
        doc.upsert_account(queue_arn)
        doc.upsert_token(
            module_slug,
            queue_arn=queue_arn,
            workspace_id=workspace_id,
            outputs=root_outputs or {},
            extra_inputs=extra_inputs,
        )
        placed_files[root_file] = render_honeytokens_tf(doc)

        if task_env and task_env.get("entries"):
            edit = self._task_env_edit(gh_repo, resolved_module_path, branch_name, task_env)
            names = [name for name, _ in task_env["entries"]]
            if edit:
                placed_files[edit["path"]] = edit["text"]
            self.task_env_result = {
                "tf_address": (edit or {}).get("tf_address") or task_env.get("tf_address"),
                "file": (edit or {}).get("path"),
                "env_vars": names,
            }

        commit_files(
            gh_repo,
            branch_name,
            placed_files,
            add_message=f"beamreach: add honeypot {honeytoken_name}",
            update_message=f"beamreach: update honeypot {honeytoken_name}",
        )

        pr_title = f"Honeypot: plant {honeytoken_class.replace('_', ' ')} — {honeytoken_name}"
        pr_body = self._plant_pr_body(honeytoken)

        self.last_pr = open_or_update_pr(gh_repo, branch=branch_name, base_branch=base_branch, title=pr_title, body=pr_body)
        logger.info("Honeypot plant PR: %s", self.last_pr.url)
        return self.last_pr.url

    def create_module_pr(
        self,
        *,
        module_name: str,
        module_dir: str,
        files: Dict[str, str],
        outputs: Optional[Dict[str, str]] = None,
        branch_suffix: str,
        title: str,
        body: str,
        resolved_repo: Optional[str] = None,
        resolved_module_path: Optional[str] = None,
    ) -> str:
        """Open a PR that adds or updates one root-level module (e.g. the
        responder role), wired from `<root>/services.tf` like the tokens are.
        Re-running updates the same branch and block in place."""
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required")
        g = github_client(token)
        repo_name = self._resolve_repo(resolved_repo)
        gh_repo = g.get_repo(repo_name)
        base_branch = self._get_base_branch()
        branch_name = f"{_BRANCH_PREFIX}{_safe_branch_suffix(branch_suffix)}"

        ensure_branch(gh_repo, branch_name, base_branch)

        placed_files = self._prefix_files_with_module(files, resolved_module_path)
        root_file = self._root_path(resolved_module_path, HONEYTOKENS_FILE)
        doc = parse_honeytokens_tf(self._read_text(gh_repo, root_file, branch_name) or "")
        doc.upsert_module(module_name, module_dir, outputs=outputs or {})
        placed_files[root_file] = render_honeytokens_tf(doc)

        commit_files(
            gh_repo,
            branch_name,
            placed_files,
            add_message=f"beamreach: add {module_name}",
            update_message=f"beamreach: update {module_name}",
        )

        self.last_pr = open_or_update_pr(gh_repo, branch=branch_name, base_branch=base_branch, title=title, body=body)
        logger.info("Module PR: %s", self.last_pr.url)
        return self.last_pr.url

    def create_replant_alerts_pr(
        self,
        *,
        queues_by_region: Dict[str, str],
        resolved_repo: Optional[str] = None,
        resolved_module_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Point every module in the root file at the queue of its region: a module whose
        `alert_queue_arn` names a queue in region R gets the new queue in R; the account
        module (one input) gets the default region's. Nothing else in the file changes.
        Returns the PR URL and what was rewritten; no PR when nothing needed it."""
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required")
        g = github_client(token)
        repo_name = self._resolve_repo(resolved_repo)
        gh_repo = g.get_repo(repo_name)
        base_branch = self._get_base_branch()
        root_file = self._root_path(resolved_module_path, HONEYTOKENS_FILE)
        text = self._read_text(gh_repo, root_file, base_branch)
        if text is None:
            return {"pr_url": None, "rewritten": [], "unchanged": [], "reason": f"{root_file} is not in {repo_name}: nothing planted yet"}
        doc = parse_honeytokens_tf(text)
        new_arns = set(queues_by_region.values())
        default_region = next(iter(queues_by_region))
        rewritten: List[Dict[str, str]] = []
        unchanged: List[str] = []
        for name, attrs in doc.modules.items():
            current = _unq(attrs.get("alert_queue_arn", ""))
            if not current or current in new_arns:
                unchanged.append(name)
                continue
            region = current.split(":")[3] if current.count(":") >= 5 else default_region
            replacement = queues_by_region.get(region) or (queues_by_region.get(default_region) if name == ACCOUNT_MODULE_NAME else None)
            if not replacement:
                unchanged.append(name)
                logger.warning("%s reports to %s and there is no new queue in %s: run `compass connect regions=%s`", name, current, region, region)
                continue
            attrs["alert_queue_arn"] = _q(replacement)
            rewritten.append({"module": name, "from": current, "to": replacement})
        if not rewritten:
            return {"pr_url": None, "rewritten": [], "unchanged": unchanged, "reason": "every module already reports to the current queues"}
        branch_name = f"{_BRANCH_PREFIX}replant-alerts"
        ensure_branch(gh_repo, branch_name, base_branch)
        commit_files(gh_repo, branch_name, {root_file: render_honeytokens_tf(doc)},
                     add_message="beamreach: decoy alerts report to the Compass queues",
                     update_message="beamreach: decoy alerts report to the Compass queues")
        lines = "\n".join(f"- `{r['module']}`: `{r['from']}` → `{r['to']}`" for r in rewritten)
        body = (
            "Decoy alerts now flow into the account Compass runs in. This changes only the `alert_queue_arn` "
            "input of each planted module (and the account module's delivery role), nothing else.\n\n"
            f"{lines}\n\nAfter the apply, the old queue receives nothing and can be deleted."
        )
        self.last_pr = open_or_update_pr(gh_repo, branch=branch_name, base_branch=base_branch,
                                         title="Decoy alerts: report to the Compass queues", body=body)
        logger.info("Replant-alerts PR: %s", self.last_pr.url)
        return {"pr_url": self.last_pr.url, "rewritten": rewritten, "unchanged": unchanged}

    def create_decommission_pr(
        self,
        *,
        honeytoken: Dict[str, Any],
        module_slug: str,
        resolved_repo: Optional[str] = None,
        resolved_module_path: Optional[str] = None,
        task_env: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Open a decommission PR: delete `<root>/services/<slug>/` and drop the
        token's module block and outputs from `services.tf`; with `task_env`
        ({container, tf_address, names}) also remove the variables the plant
        PR put into the customer's task definition.

        The once-per-account `svc_account` module and `services/_account/` are
        deliberately left in place: other tokens in the account still use the
        delivery role, and removing it would break their alert delivery."""
        token = resolve_github_token(self.config, capability="write")
        if not token:
            raise RuntimeError("GitHub write token is required")

        g = github_client(token)
        repo_name = self._resolve_repo(resolved_repo)
        gh_repo = g.get_repo(repo_name)
        base_branch = self._get_base_branch()

        honeytoken_name = str(honeytoken.get("name") or default_cover_name(str(honeytoken.get("honeytoken_id", ""))))
        branch_name = f"{_BRANCH_PREFIX}decommission-{_safe_branch_suffix(honeytoken_name)}"

        ensure_branch(gh_repo, branch_name, base_branch)

        # The task definition first: it reads the module's outputs.
        if task_env and task_env.get("names") and task_env.get("container"):
            edit = self._task_env_edit(gh_repo, resolved_module_path, branch_name, task_env, remove=True)
            if edit:
                current = self._read_contents(gh_repo, edit["path"], branch_name)
                gh_repo.update_file(
                    edit["path"], f"beamreach: remove honeypot {honeytoken_name}", edit["text"], current.sha, branch=branch_name
                )

        # Module block next, so a half-applied PR never references a deleted directory.
        root_file = self._root_path(resolved_module_path, HONEYTOKENS_FILE)
        existing = self._read_contents(gh_repo, root_file, branch_name)
        if existing is not None:
            doc = parse_honeytokens_tf(existing.decoded_content.decode("utf-8"))
            if doc.remove_token(module_slug):
                gh_repo.update_file(
                    root_file,
                    f"beamreach: remove honeypot {honeytoken_name}",
                    render_honeytokens_tf(doc),
                    existing.sha,
                    branch=branch_name,
                )

        module_dir = self._root_path(resolved_module_path, f"{SERVICES_DIR}/{module_slug}")
        for file_path in self._list_files(gh_repo, module_dir, branch_name):
            existing_file = self._read_contents(gh_repo, file_path, branch_name)
            if existing_file is None:
                continue
            gh_repo.delete_file(
                file_path,
                f"beamreach: remove honeypot {honeytoken_name}",
                existing_file.sha,
                branch=branch_name,
            )

        pr_title = f"Honeypot: decommission {honeytoken_name}"
        pr_body = (
            f"## Decommission honeypot: {honeytoken_name}\n\n"
            f"- **Honeytoken class:** {honeytoken.get('honeytoken_class', '').replace('_', ' ')}\n"
            f"- **Honeytoken ID:** `{honeytoken.get('honeytoken_id', '')}`\n\n"
            "This PR removes the honeytoken infrastructure. Merge to complete decommissioning."
        )

        self.last_pr = open_or_update_pr(gh_repo, branch=branch_name, base_branch=base_branch, title=pr_title, body=pr_body)
        return self.last_pr.url

    @staticmethod
    def _plant_pr_body(honeytoken: Dict[str, Any]) -> str:
        action_config = honeytoken.get("action_config") or {}
        tags = honeytoken.get("tags") or {}
        lines = [
            f"## Honeypot: {honeytoken.get('name') or default_cover_name(str(honeytoken.get('honeytoken_id', '')))}",
            "",
            f"- **Honeytoken class:** {str(honeytoken.get('honeytoken_class', '')).replace('_', ' ')}",
            f"- **Honeytoken ID:** `{honeytoken.get('honeytoken_id', '')}`",
            f"- **Severity:** {action_config.get('severity', 'high')}",
            f"- **Location:** {honeytoken.get('region', '—')} / {honeytoken.get('account_id', '—')}",
            "",
            "### What this does",
            "",
            "Deploys a honeytoken that should never be accessed in normal operations.",
            "Any access triggers an immediate alert via the Beamreach Honeypots system.",
            "",
            "### Tags applied to all resources",
            "",
        ]
        for k, v in tags.items():
            lines.append(f"- `{k}` = `{v}`")
        lines += [
            "",
            "### Action runbook",
            "",
            "```",
            action_config.get("runbook", "See the Honeypots tab in Compass."),
            "```",
            "",
            "> This PR was generated automatically by [Compass](https://github.com/beamreach-ai/compass). "
            "Do not remove the `cost-center` tag — it links this resource to the honeypot tracking system.",
        ]
        return "\n".join(lines)
