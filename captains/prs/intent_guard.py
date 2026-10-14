"""The deterministic check between the model and the push.

``evaluate`` looks at the working tree after a revision ran and answers one
question: does the PR still do what its intent says? It never calls a model.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_BLOCKERS_RE = re.compile(r"^\s*BLOCKERS:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class GuardResult:
    ok: bool
    reason: Optional[str]
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "checks": list(self.checks)}


def _git(repo_root: Path, args: List[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )


def changed_files_vs_base(repo_root: Path, base_ref: str) -> List[str]:
    """Files that differ between the base branch and the working tree (committed or not)."""
    out = _git(repo_root, ["diff", "--name-only", base_ref])
    files = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    untracked = _git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    for line in (untracked.stdout or "").splitlines():
        if line.strip() and line.strip() not in files:
            files.append(line.strip())
    return files


def _read(repo_root: Path, rel: str) -> str:
    try:
        return (repo_root / rel).read_text()
    except Exception:
        return ""


def _scope_text(repo_root: Path, files: Iterable[str]) -> str:
    return "\n".join(_read(repo_root, f) for f in files)


def _address_parts(address: str):
    head = address.split("[", 1)[0]
    if "." not in head:
        return None
    rtype, name = head.split(".", 1)
    return rtype, name


def check_invariant(repo_root: Path, inv: Dict[str, Any], *, base_ref: str, scope_files: List[str]) -> Dict[str, Any]:
    kind = str(inv.get("kind") or "")
    result = {"name": kind, "ok": False, "detail": None}
    try:
        if kind == "files_differ_from_base":
            files = [f for f in (inv.get("files") or scope_files) if f]
            if not files:
                diff = _git(repo_root, ["diff", "--quiet", base_ref])
                result["ok"] = diff.returncode != 0
                result["detail"] = "working tree differs from base" if result["ok"] else "no change against base"
            else:
                diff = _git(repo_root, ["diff", "--quiet", base_ref, "--", *files])
                missing = [f for f in files if not (repo_root / f).exists()]
                untracked_new = any(f in changed_files_vs_base(repo_root, base_ref) for f in files)
                result["ok"] = diff.returncode != 0 or untracked_new
                result["detail"] = ("reverted: " + ", ".join(files)) if not result["ok"] else None
                if missing and result["ok"]:
                    result["detail"] = "deleted: " + ", ".join(missing)
        elif kind in ("resource_blocks_present", "import_blocks_present"):
            text = _scope_text(repo_root, scope_files) if scope_files else _scope_text(repo_root, changed_files_vs_base(repo_root, base_ref))
            missing = []
            for address in inv.get("addresses") or []:
                parts = _address_parts(str(address))
                if not parts:
                    continue
                rtype, name = parts
                if kind == "resource_blocks_present":
                    pattern = re.compile(r'resource\s+"%s"\s+"%s"\s*\{' % (re.escape(rtype), re.escape(name)))
                else:
                    pattern = re.compile(r'to\s*=\s*%s' % re.escape(str(address).split("[", 1)[0]))
                if not pattern.search(text):
                    missing.append(str(address))
            result["ok"] = not missing
            result["detail"] = ("missing: " + ", ".join(missing)) if missing else None
        elif kind in ("module_block_present", "module_block_absent"):
            text = _read(repo_root, str(inv.get("file") or ""))
            present = re.search(r'module\s+"%s"\s*\{' % re.escape(str(inv.get("module") or "")), text) is not None
            result["ok"] = present if kind == "module_block_present" else not present
            result["detail"] = None if result["ok"] else f'module "{inv.get("module")}" {"missing from" if kind == "module_block_present" else "still in"} {inv.get("file")}'
        elif kind in ("path_present", "path_absent"):
            exists = (repo_root / str(inv.get("path") or "")).exists()
            result["ok"] = exists if kind == "path_present" else not exists
            result["detail"] = None if result["ok"] else f"{inv.get('path')} {'missing' if kind == 'path_present' else 'still present'}"
        else:
            result["ok"] = True
            result["detail"] = f"unknown invariant kind {kind!r} ignored"
    except Exception as exc:  # a broken check is a failed check
        result["ok"] = False
        result["detail"] = f"check failed: {exc}"
    return result


def parse_blockers(final_text: Optional[str]) -> List[str]:
    """The runtime ends its final message with ``BLOCKERS: none`` or ``BLOCKERS: <id>: why; ...``."""
    if not isinstance(final_text, str):
        return []
    match = _BLOCKERS_RE.search(final_text)
    if not match:
        return []
    body = match.group(1).strip()
    if not body or body.lower().rstrip(".") in ("none", "no", "-", "n/a"):
        return []
    return [part.strip() for part in body.split(";") if part.strip()]


def evaluate(
    repo_root: Path,
    *,
    base_ref: str,
    intent: Dict[str, Any],
    scope_files: List[str],
    requested_files: List[str],
    execution_state: Dict[str, Any],
    final_text: Optional[str] = None,
) -> GuardResult:
    checks: List[Dict[str, Any]] = []
    changed = [f for f in (execution_state.get("changed_files") or []) if isinstance(f, str)]
    if not changed:
        changed = changed_files_vs_base(repo_root, base_ref)

    allowed = set(scope_files) | set(requested_files)
    outside = [f for f in changed if allowed and f not in allowed]
    checks.append({"name": "scope", "ok": not outside, "detail": ("outside scope: " + ", ".join(outside)) if outside else None})

    for inv in intent.get("invariants") or []:
        if isinstance(inv, dict):
            checks.append(check_invariant(repo_root, inv, base_ref=base_ref, scope_files=scope_files))

    errors = [
        v for v in (execution_state.get("validation") or [])
        if isinstance(v, dict) and str(v.get("status") or "").lower() == "error"
    ]
    checks.append({
        "name": "verification",
        "ok": not errors,
        "detail": ("; ".join(f"{e.get('dir') or '.'}: {(e.get('stderr') or e.get('error') or 'error')[:200]}" for e in errors)) if errors else None,
    })

    non_empty = _git(repo_root, ["diff", "--quiet", base_ref]).returncode != 0 or bool(changed_files_vs_base(repo_root, base_ref))
    checks.append({"name": "diff_non_empty", "ok": non_empty, "detail": None if non_empty else "no change against base"})

    blockers = parse_blockers(final_text)
    checks.append({"name": "blockers", "ok": not blockers, "detail": "; ".join(blockers) if blockers else None})

    reason: Optional[str] = None
    for check in checks:
        if check["ok"]:
            continue
        name = check["name"]
        if name == "scope":
            reason = "scope_expanded"
        elif name == "verification":
            reason = "verification_failed"
        elif name == "diff_non_empty":
            reason = "no_change"
        elif name == "blockers":
            reason = "blocked_by_runtime"
        else:
            reason = "invariant_broken"
        break
    return GuardResult(ok=reason is None, reason=reason, checks=checks)
