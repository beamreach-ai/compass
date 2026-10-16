"""The open edition's revision runner: Claude through the Anthropic SDK's tool runner.

``execute`` hands the checkout to the model with four tools (read, write, list,
a terraform check) and the execution contract as its brief; ``publish`` commits
the files the contract allows and pushes them to the pull request's branch.
Both fill the state the way ``captains.prs.revision.run_revision`` and
``captains.prs.intent_guard`` read it, so the guard between them is unchanged.

The SDK is the optional ``anthropic`` extra and nothing here imports it until a
revision runs; ``revision_runner()`` only builds this runner when
``compass_core.anthropic_model.available()`` says a credential is in reach.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from captains.prs import intent_guard
from captains.prs import sync as sync_rules
from captains.prs.revision import REVISION_RULES, RevisionState, _git
from compass_core import anthropic_model
from compass_core.github import resolve_github_token_for_repo

logger = logging.getLogger("prs.claude_runner")

RUNNER_NAME = "anthropic_tool_runner"
MAX_TOKENS = 16000
READ_LIMIT = 200_000  # characters of one file the model may read
LIST_LIMIT = 500
_TERRAFORM_SCRATCH = (".terraform", ".terraform.lock.hcl")  # what `terraform init` leaves behind; never part of the change
_BLOCKERS_LINE = re.compile(r"^\s*BLOCKERS:", re.IGNORECASE | re.MULTILINE)

_TOOL_GUIDANCE = (
    "You are working in a git checkout of the pull request branch. Use `list_files` and `read_file` to look, "
    "`write_file` to change a file (whole content; only files in `patch_spec.allowed_files` are accepted) and "
    "`run` for `terraform fmt -check` / `terraform validate`. Make the change the reviewers asked for, then answer "
    "with a short summary of what you changed."
)


def _sdk() -> Tuple[Any, Any]:
    """``(beta_tool, ToolError)`` from the SDK; ImportError when the extra is not installed."""
    from anthropic import beta_tool
    from anthropic.lib.tools import ToolError

    return beta_tool, ToolError


def _terraform_bin() -> Optional[str]:
    return shutil.which("terraform")


def push_url(repo: str, token: str) -> str:
    from compass_core.github import git_url

    return git_url(repo, token)


def _is_scratch(path: str) -> bool:
    return any(part in _TERRAFORM_SCRATCH for part in Path(path).parts)


def changed_files(repo_root: Path) -> List[str]:
    """Working-tree changes against HEAD: what the model did, tracked or new."""
    tracked = _git(repo_root, ["diff", "--name-only"], check=False).stdout or ""
    untracked = _git(repo_root, ["ls-files", "--others", "--exclude-standard"], check=False).stdout or ""
    files = [line.strip() for line in (tracked + untracked).splitlines() if line.strip()]
    return [f for f in dict.fromkeys(files) if not _is_scratch(f)]


class _Workspace:
    """The tools, bound to one checkout. Every path must resolve inside it; writes must be allowed."""

    def __init__(self, root: Path, allowed: List[str], *, error: type = RuntimeError) -> None:
        self.root = root.resolve()
        self.allowed = set(allowed)
        self.error = error
        self.written: List[str] = []
        self.runs: List[Dict[str, Any]] = []

    def _resolve(self, path: str) -> Tuple[Path, str]:
        candidate = (self.root / str(path or "")).resolve()
        try:
            rel = candidate.relative_to(self.root).as_posix()
        except ValueError:
            raise self.error(f"{path!r} is outside the checkout")
        if rel == "." or rel.startswith(".git/") or rel == ".git":
            raise self.error(f"{path!r} is not a file in the checkout")
        return candidate, rel

    def read_file(self, path: str) -> str:
        """Read one file of the checkout. Call it before changing a file, so the edit keeps everything else as it is.

        Args:
            path: Path relative to the checkout root, e.g. modules/storage.tf.
        """
        target, rel = self._resolve(path)
        if not target.is_file():
            raise self.error(f"{rel} does not exist")
        text = target.read_text(errors="replace")
        if len(text) > READ_LIMIT:
            return text[:READ_LIMIT] + f"\n…[truncated, {len(text)} characters in total]"
        return text

    def write_file(self, path: str, content: str) -> str:
        """Replace the whole content of one file. Only the files in `patch_spec.allowed_files` are accepted;
        anything else is refused, and you explain it on the BLOCKERS line instead.

        Args:
            path: Path relative to the checkout root; must be one of patch_spec.allowed_files.
            content: The complete new content of the file.
        """
        target, rel = self._resolve(path)
        if rel not in self.allowed:
            raise self.error(f"{rel} is not in patch_spec.allowed_files ({', '.join(sorted(self.allowed)) or 'none'}); not written")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if rel not in self.written:
            self.written.append(rel)
        return f"wrote {rel} ({len(content)} characters)"

    def list_files(self, glob: str = "**/*") -> str:
        """List files of the checkout matching a glob, one path per line.

        Args:
            glob: A glob relative to the checkout root, e.g. modules/*.tf or **/*.tf.
        """
        pattern = str(glob or "**/*").lstrip("/")
        found: List[str] = []
        for match in sorted(self.root.glob(pattern)):
            if not match.is_file():
                continue
            rel = match.relative_to(self.root).as_posix()
            if rel.startswith(".git/") or _is_scratch(rel):
                continue
            found.append(rel)
            if len(found) >= LIST_LIMIT:
                found.append(f"…[more than {LIST_LIMIT} files; narrow the glob]")
                break
        return "\n".join(found) or "(no files match)"

    def run(self, cmd: str, dir: str = ".") -> str:
        """Run a terraform check in a directory of the checkout. Only `terraform fmt -check [paths]` and
        `terraform validate` are allowed; nothing else runs. Returns the exit code and output.

        Args:
            cmd: `terraform fmt -check` (optionally with -diff and file paths) or `terraform validate`.
            dir: Directory relative to the checkout root to run in, default the root.
        """
        argv = shlex.split(str(cmd or ""))
        if argv[:1] != ["terraform"] or len(argv) < 2:
            raise self.error("only `terraform fmt -check` and `terraform validate` may be run")
        if argv[1] == "fmt":
            flags = {a for a in argv[2:] if a.startswith("-")}
            paths = [a for a in argv[2:] if not a.startswith("-")]
            if "-check" not in flags or not flags <= {"-check", "-diff", "-recursive", "-no-color"}:
                raise self.error("terraform fmt may only be run with -check (and -diff, -recursive, -no-color)")
            for p in paths:
                self._resolve(os.path.join(dir, p))
        elif argv[1] != "validate" or not set(argv[2:]) <= {"-no-color"}:
            raise self.error("only `terraform fmt -check` and `terraform validate` may be run")
        cwd, rel = (self.root, ".") if str(dir or ".") in (".", "") else self._resolve(dir)
        if not cwd.is_dir():
            raise self.error(f"{rel} is not a directory")
        binary = _terraform_bin()
        if binary is None:
            return "terraform is not installed here; the check was skipped"
        command = [binary, *argv[1:]] + ([] if "-no-color" in argv else ["-no-color"])
        proc = subprocess.run(command, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180, check=False)
        self.runs.append({"cmd": " ".join(argv), "dir": rel, "returncode": proc.returncode})
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return f"exit {proc.returncode}\n{out}\n{err}".strip()

    def tools(self) -> List[Any]:
        return [self.read_file, self.write_file, self.list_files, self.run]


# ── verification after the model is done (the same checks the product's node runs) ──


def terraform_fmt(repo_root: Path, paths: List[str]) -> Dict[str, Any]:
    paths = [p for p in paths if p.endswith((".tf", ".tfvars")) and (repo_root / p).is_file()]
    if not paths:
        return {"status": "skipped", "paths": []}
    binary = _terraform_bin()
    if binary is None:
        return {"status": "skipped", "paths": paths, "reason": "terraform_missing"}
    try:
        proc = subprocess.run([binary, "fmt", "-write=true", *paths], cwd=str(repo_root), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=120, check=False)
    except Exception as exc:
        return {"status": "error", "paths": paths, "error": str(exc)}
    return {"status": "completed" if proc.returncode == 0 else "error", "paths": paths,
            "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip()}


def terraform_validate(repo_root: Path, dirs: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    binary = _terraform_bin()
    for directory in dirs or []:
        resolved = (repo_root / directory).resolve()
        if not resolved.is_dir() or not any(c.suffix == ".tf" for c in resolved.iterdir() if c.is_file()):
            results.append({"dir": directory, "status": "skipped", "reason": "no_terraform_files"})
            continue
        if binary is None:
            results.append({"dir": directory, "status": "skipped", "reason": "terraform_missing"})
            continue
        try:
            # validate needs the providers resolved; a backend-less init is enough and touches no state
            init = subprocess.run([binary, "init", "-backend=false", "-input=false", "-no-color"], cwd=str(resolved),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180, check=False)
            if init.returncode != 0:
                results.append({"dir": directory, "status": "error", "error": "terraform init failed", "stderr": (init.stderr or "").strip()})
                continue
            proc = subprocess.run([binary, "validate", "-no-color"], cwd=str(resolved), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=180, check=False)
        except Exception as exc:
            results.append({"dir": directory, "status": "error", "error": str(exc)})
            continue
        results.append({"dir": directory, "status": "completed" if proc.returncode == 0 else "error",
                        "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip()})
    return results


# ── the runner ──


def _system_prompt(contract: Dict[str, Any]) -> str:
    brief = {k: contract.get(k) for k in ("intent", "origin_context", "review_feedback", "patch_spec")}
    rules = "\n".join(f"- {rule}" for rule in REVISION_RULES)
    return f"{rules}\n\n{_TOOL_GUIDANCE}\n\nExecution contract:\n```json\n{json.dumps(brief, indent=2, default=str)}\n```"


def _user_message(state: RevisionState, contract: Dict[str, Any]) -> str:
    pr = (state.context or {}).get("pr") or {}
    lines = [f"Revise pull request {pr.get('repo')}#{pr.get('number')} on branch `{contract.get('target', {}).get('branch')}`."]
    instructions = [str(i) for i in (contract.get("instructions") or []) if str(i).strip()]
    if instructions:
        lines.append("Requested:")
        lines.extend(f"- {i}" for i in instructions)
    lines.append("When you are done, summarise the change and end with the BLOCKERS line.")
    return "\n".join(lines)


def _final_text(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in (getattr(message, "content", None) or []) if getattr(b, "type", None) == "text")


def _add_usage(total: Dict[str, int], message: Any) -> None:
    usage = getattr(message, "usage", None)
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _subject(final_text: Optional[str], fallback: str) -> str:
    """The first line of the model's answer that is not the BLOCKERS line, cut to a git subject."""
    for line in (final_text or "").splitlines():
        text = " ".join(line.strip().lstrip("#-* ").split())
        if text and not text.upper().startswith("BLOCKERS:"):
            return text if len(text) <= 68 else text[:67] + "…"
    return fallback


class ClaudeRevisionRunner:
    """A ``RevisionRunner`` (captains.prs.revision) on the Anthropic SDK's beta tool runner."""

    def __init__(self, *, client: Any = None, model: Optional[str] = None) -> None:
        self._client = client
        self._model = model

    # ── execute ──

    def _finish(self, state: RevisionState, contract: Dict[str, Any], execution_state: Dict[str, Any], runtime_result: Optional[Dict[str, Any]]) -> RevisionState:
        contract = dict(contract)
        contract["status"] = execution_state["status"]
        contract["runtime_result"] = runtime_result
        contract["last_updated_at"] = int(time.time())
        if execution_state.get("error"):
            contract["error"] = execution_state["error"]
        state.execution_contract = contract
        state.execution_state = execution_state
        changed = execution_state.get("changed_files") or []
        state.answer = (
            f"Error: {execution_state['error']}" if execution_state.get("error")
            else ("Changed files: " + ", ".join(changed) if changed else "No files changed.")
        )
        return state

    def _unavailable(self, state: RevisionState, contract: Dict[str, Any], repo_root: Path, why: str) -> RevisionState:
        # run_revision turns `runtime_unavailable` into `no_revision_runner`: the installation cannot revise.
        execution_state = {"status": "error", "error_type": "runtime_unavailable", "error": why, "mode": "claude_execution",
                           "repo_path": str(repo_root), "changed_files": [], "validation": [], "format": {"status": "skipped"}}
        return self._finish(state, contract, execution_state, None)

    def execute(self, state: RevisionState, *, target: str, config: Dict[str, Any], model: Any, logger: logging.Logger) -> RevisionState:
        contract = dict(state.execution_contract or {})
        target_block = contract.get("target") if isinstance(contract.get("target"), dict) else {}
        repo_root = Path(str(target_block.get("workspace_root") or (state.target_state or {}).get("local_repo_path") or "."))
        patch_spec = contract.get("patch_spec") if isinstance(contract.get("patch_spec"), dict) else {}
        allowed = [f for f in (patch_spec.get("allowed_files") or []) if isinstance(f, str)]
        verification = contract.get("verification") if isinstance(contract.get("verification"), dict) else {}
        options = contract.get("runtime_options") if isinstance(contract.get("runtime_options"), dict) else {}

        try:
            beta_tool, tool_error = _sdk()
        except ImportError as exc:
            return self._unavailable(state, contract, repo_root, f"the anthropic SDK is not installed ({exc}); pip install compass-open[anthropic]")
        if self._client is None and not anthropic_model.available():
            return self._unavailable(state, contract, repo_root, "no Anthropic credential: set ANTHROPIC_API_KEY")
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        model_id = self._model or anthropic_model._settings(config)["model"]  # the same setting the text model honours

        workspace = _Workspace(repo_root, allowed, error=tool_error)
        usage: Dict[str, int] = {}
        turns = 0
        last: Any = None
        try:
            runner = self._client.beta.messages.tool_runner(
                model=model_id,
                max_tokens=MAX_TOKENS,
                system=_system_prompt(contract),
                messages=[{"role": "user", "content": _user_message(state, contract)}],
                tools=[beta_tool(fn) for fn in workspace.tools()],
                thinking={"type": "adaptive"},
                max_iterations=int(options.get("max_turns") or 20),
            )
            for message in runner:
                last = message
                turns += 1
                _add_usage(usage, message)
        except Exception as exc:
            logger.warning("[prs.claude_runner] runtime failed after %s turn(s): %s", turns, exc)
            execution_state = {"status": "error", "error_type": "runtime_error", "error": f"{type(exc).__name__}: {exc}"[:500],
                               "mode": "claude_execution", "repo_path": str(repo_root), "changed_files": changed_files(repo_root),
                               "validation": [], "format": {"status": "skipped"}}
            return self._finish(state, contract, execution_state, {"runner": RUNNER_NAME, "model": model_id, "usage": usage, "turns": turns})

        final_text = _final_text(last)
        stop_reason = getattr(last, "stop_reason", None)
        blockers = intent_guard.parse_blockers(final_text)
        changed = changed_files(repo_root)
        runtime_result = {
            "runner": RUNNER_NAME, "model": model_id, "final_text": final_text, "stop_reason": stop_reason,
            "files_written": list(workspace.written), "blockers": blockers, "usage": usage, "turns": turns, "runs": workspace.runs,
        }
        fmt = terraform_fmt(repo_root, [p for p in (verification.get("format_paths") or []) if isinstance(p, str)] or changed)
        validation = terraform_validate(repo_root, [d for d in (verification.get("validate_dirs") or []) if isinstance(d, str)])
        changed = changed_files(repo_root)  # fmt may have rewritten files

        error: Optional[str] = None
        error_type: Optional[str] = None
        if stop_reason == "refusal":
            error, error_type = "the model declined the request", "refusal"
        elif last is None or not _BLOCKERS_LINE.search(final_text):
            error, error_type = f"the runtime stopped ({stop_reason or 'no message'}) without a BLOCKERS line", "no_final_answer"
        elif not changed and not blockers:
            # The guard reports blockers itself; only a silent no-op is an execution failure.
            error, error_type = "the model made no change", "no_diff"
        execution_state = {
            "status": "error" if error else "completed",
            "mode": "claude_execution",
            "repo_path": str(repo_root),
            "branch": target_block.get("branch") or (state.target_state or {}).get("branch"),
            "changed_files": changed,
            "diff_name_only": changed,
            "diff_stat": (_git(repo_root, ["diff", "--stat"], check=False).stdout or "").strip(),
            "format": fmt,
            "validation": validation,
            "runtime": runtime_result,
            "error": error,
            "error_type": error_type,
        }
        logger.info("[prs.claude_runner] %s: %s file(s) changed, %s turn(s), blockers=%s", execution_state["status"], len(changed), turns, len(blockers))
        return self._finish(state, contract, execution_state, runtime_result)

    # ── publish ──

    def publish(self, state: RevisionState, *, target: str, config: Dict[str, Any], logger: logging.Logger, workspace_id: Optional[str] = None) -> RevisionState:
        patch_state = dict(state.patch_state or {})
        target_state = dict(state.target_state or {})
        repo = str(patch_state.get("repo") or target_state.get("repo") or "")
        branch = str(patch_state.get("branch") or target_state.get("branch") or "")
        base_branch = str(patch_state.get("base_branch") or target_state.get("default_branch") or "")
        repo_root = Path(str(patch_state.get("repo_path") or target_state.get("local_repo_path") or "."))
        allowed = [f for f in (patch_state.get("allowed_files") or []) if isinstance(f, str)]
        pr_url = ((state.context or {}).get("pr") or {}).get("url")

        def fail(message: str, **extra: Any) -> RevisionState:
            state.publish_state = {"status": extra.pop("status", "error"), "error": message, "repo": repo, "branch": branch, **extra}
            return state

        if not repo or not branch:
            return fail("the state has no repo or branch to push to")
        if branch == base_branch:
            return fail(f"refusing to push to the base branch {base_branch!r}")
        to_commit = [f for f in changed_files(repo_root) if f in set(allowed)]
        if not to_commit:
            return fail(f"nothing to commit on {branch}", status="nothing_to_commit", changed_files=[])
        token = resolve_github_token_for_repo(config, repo_slug=repo, capability="write")
        if not token:
            return fail(f"no GitHub write token for {repo}")

        runtime = (state.execution_contract or {}).get("runtime_result") or {}
        subject = "compass: " + _subject(runtime.get("final_text") if isinstance(runtime, dict) else None, str(patch_state.get("summary") or f"revise {branch}"))
        body = f"Revises {pr_url or f'{repo}#{((state.context or {}).get('pr') or {}).get('number')}'} after review."
        github = config.get("github") if isinstance(config.get("github"), dict) else {}
        # the watcher recognises its own commits by this address (captains.prs.sync.compass_identity)
        email = str(github.get("author_email") or sync_rules.DEFAULT_AUTHOR_EMAIL)
        step = "add"
        try:
            _git(repo_root, ["add", "-A", "--", *to_commit])
            step = "commit"
            _git(repo_root, ["-c", "user.name=Compass", "-c", f"user.email={email}", "commit", "-m", subject, "-m", body])
            sha = (_git(repo_root, ["rev-parse", "HEAD"]).stdout or "").strip()
            step = "push"
            # a plain push: the branch moves forward by one commit or the push is refused, never rewritten
            _git(repo_root, ["push", push_url(repo, token), f"HEAD:refs/heads/{branch}"], timeout=300)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).replace(token, "***")
            logger.warning("[prs.claude_runner] git %s for %s %s failed: %s", step, repo, branch, detail[:300])
            return fail(f"git {step} failed: {detail[:400]}", changed_files=to_commit)

        state.publish_state = {
            "status": "completed", "repo": repo, "branch": branch, "base_branch": base_branch, "commit_sha": sha,
            "pr_url": pr_url, "changed_files": to_commit, "published_at": int(time.time()),
        }
        state.answer = f"{state.answer or ''}\nPushed {sha[:7]} to {branch}.".strip()
        logger.info("[prs.claude_runner] pushed %s to %s %s (%s file(s))", sha[:7], repo, branch, len(to_commit))
        return state
