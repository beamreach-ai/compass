"""Intent records: what a pull request must keep doing, captured when it opens.

Each builder returns the same shape so the guard (``intent_guard``) and the
revision prompt do not care which captain opened the PR::

    goal, action_family, done_criteria, safe_change_class, rollback_expectation,
    patch_summary, scope{files, dirs, resource_ids}, invariants[], verification{},
    risk, contract (engine PRs only: the execution contract the run used)

Invariants are checkable without a model; ``intent_guard.check_invariant``
knows every ``kind`` listed here.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional

_ADDRESS_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_-]+(\[[^\]]+\])?$")


def _dirs(files: Iterable[str]) -> List[str]:
    out: List[str] = []
    for path in files:
        directory = os.path.dirname(path) or "."
        if directory not in out:
            out.append(directory)
    return out


def _clean_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def _base(
    *,
    goal: str,
    action_family: str,
    done_criteria: List[str],
    safe_change_class: Optional[str],
    rollback_expectation: List[str],
    patch_summary: Optional[str],
    files: List[str],
    resource_ids: List[str],
    invariants: List[Dict[str, Any]],
    verification: Optional[Dict[str, Any]],
    risk: Optional[int],
) -> Dict[str, Any]:
    files = _clean_list(files)
    return {
        "goal": goal,
        "action_family": action_family,
        "done_criteria": list(done_criteria),
        "safe_change_class": safe_change_class,
        "rollback_expectation": list(rollback_expectation),
        "patch_summary": patch_summary,
        "scope": {"files": files, "dirs": _dirs(files), "resource_ids": _clean_list(resource_ids)},
        "invariants": list(invariants),
        "verification": dict(verification or {"validate_dirs": _dirs(files), "format_paths": files}),
        "risk": int(risk) if isinstance(risk, (int, float)) else None,
    }


def intent_from_contract(contract: Dict[str, Any], changed_files: List[str], *, risk: Optional[int] = None) -> Dict[str, Any]:
    """Security and FinOps fixes: the execution contract already says it all."""
    contract = contract if isinstance(contract, dict) else {}
    intent = contract.get("intent") if isinstance(contract.get("intent"), dict) else {}
    patch_spec = contract.get("patch_spec") if isinstance(contract.get("patch_spec"), dict) else {}
    target = contract.get("target") if isinstance(contract.get("target"), dict) else {}
    files = _clean_list(changed_files) or _clean_list(patch_spec.get("target_files")) or _clean_list(target.get("likely_entry_files"))
    verification = dict(contract.get("verification") or {}) if isinstance(contract.get("verification"), dict) else {}
    verification.setdefault("validate_dirs", _dirs(files))
    verification.setdefault("format_paths", files)
    record = _base(
        goal=str(intent.get("goal") or "Keep the remediation this pull request introduces"),
        action_family=str(intent.get("action_family") or "remediation"),
        done_criteria=_clean_list(intent.get("done_criteria")),
        safe_change_class=intent.get("safe_change_class"),
        rollback_expectation=_clean_list(intent.get("rollback_expectation")),
        patch_summary=patch_spec.get("summary") if isinstance(patch_spec.get("summary"), str) else None,
        files=files,
        resource_ids=_clean_list([target.get("resource_id")]) if target.get("resource_id") else [],
        invariants=[{"kind": "files_differ_from_base", "files": files}],
        verification=verification,
        risk=risk,
    )
    stored = dict(contract)
    for key in ("runtime_result", "execution_attempts", "repair_feedback", "repair_attempt", "error_details"):
        stored.pop(key, None)
    record["contract"] = stored
    return record


def intent_for_iac_import(
    *,
    addresses: List[str],
    resource_ids: List[str],
    files: List[str],
    verified: bool,
    plan_clean: bool,
    has_import_blocks: bool = True,
) -> Dict[str, Any]:
    addresses = [a for a in _clean_list(addresses) if _ADDRESS_RE.match(a)]
    invariants: List[Dict[str, Any]] = [{"kind": "resource_blocks_present", "addresses": addresses}]
    if has_import_blocks:
        invariants.append({"kind": "import_blocks_present", "addresses": addresses})
    return _base(
        goal=f"Bring {len(addresses) or len(resource_ids)} unmanaged resource(s) under Terraform without changing them",
        action_family="iac_import",
        done_criteria=[
            "Every selected resource has a resource block and an import block.",
            "terraform plan is import-only: nothing to add, change or destroy.",
        ],
        safe_change_class="import",
        rollback_expectation=["Revert the merge; state is only touched by the customer's own apply."],
        patch_summary="Terraform import of selected resources",
        files=files,
        resource_ids=resource_ids,
        invariants=invariants,
        verification=None,
        risk=1 if (verified and plan_clean) else 2,
    )


def intent_for_fix_drift(*, drift_data: List[Dict[str, Any]], files: List[str]) -> Dict[str, Any]:
    addresses = [str(d.get("resource_address")) for d in drift_data if isinstance(d, dict) and d.get("resource_address")]
    fields = sorted({str(f) for d in drift_data if isinstance(d, dict) for f in (d.get("drift_fields") or [])})
    return _base(
        goal=f"Align Terraform with live values for {len(drift_data)} drifted resource(s)",
        action_family="fix_drift",
        done_criteria=[f"The drifted attributes ({', '.join(fields[:10]) or 'reported fields'}) carry the live values."],
        safe_change_class="drift",
        rollback_expectation=["Revert the merge; the previous configuration is restored by the customer's apply."],
        patch_summary="Drift fix",
        files=files,
        resource_ids=[str(d.get("resource_id")) for d in drift_data if isinstance(d, dict) and d.get("resource_id")],
        invariants=[{"kind": "files_differ_from_base", "files": _clean_list(files)}] + (
            [{"kind": "resource_blocks_present", "addresses": [a for a in addresses if _ADDRESS_RE.match(a)]}] if addresses else []
        ),
        verification=None,
        risk=2,
    )


def intent_for_honeypot(
    command: str,
    *,
    honeytoken: Dict[str, Any],
    module_name: str,
    root_file: str,
    module_dir: Optional[str],
    files: List[str],
) -> Dict[str, Any]:
    name = str(honeytoken.get("name") or honeytoken.get("honeytoken_id") or "honeytoken")
    token_class = str(honeytoken.get("honeytoken_class") or "")
    if command == "decommission":
        invariants = [{"kind": "module_block_absent", "module": module_name, "file": root_file}]
        if module_dir:
            invariants.append({"kind": "path_absent", "path": module_dir})
        goal = f"Remove the honeytoken module for {name} and nothing else"
        done = ["The module block and its directory are gone; no other module changes."]
    else:
        invariants = [{"kind": "module_block_present", "module": module_name, "file": root_file}]
        if module_dir:
            invariants.append({"kind": "path_present", "path": module_dir})
        goal = f"Plant the {token_class or 'honeytoken'} module for {name} so its use raises an alert"
        done = [
            f'The root file loads module "{module_name}" and the module directory exists.',
            "terraform validate passes on the touched root.",
        ]
    return _base(
        goal=goal,
        action_family=f"honeypot_{command}",
        done_criteria=done,
        safe_change_class="honeypot",
        rollback_expectation=["Revert the merge; the decommission PR removes the module cleanly."],
        patch_summary=f"Honeypot {command}: {name}",
        files=files,
        resource_ids=[str(honeytoken.get("honeytoken_id") or "")] if honeytoken.get("honeytoken_id") else [],
        invariants=invariants,
        verification=None,
        risk=1,
    )


def describe_invariant(inv: Dict[str, Any]) -> str:
    kind = str(inv.get("kind") or "")
    if kind == "files_differ_from_base":
        return "Keeps the change in: " + ", ".join(inv.get("files") or []) if inv.get("files") else "Keeps a non-empty change against the base branch"
    if kind == "resource_blocks_present":
        return "Keeps resource blocks for: " + ", ".join(inv.get("addresses") or [])
    if kind == "import_blocks_present":
        return "Keeps import blocks for: " + ", ".join(inv.get("addresses") or [])
    if kind == "module_block_present":
        return f'Keeps module "{inv.get("module")}" in {inv.get("file")}'
    if kind == "module_block_absent":
        return f'Keeps module "{inv.get("module")}" out of {inv.get("file")}'
    if kind == "path_present":
        return f"Keeps {inv.get('path')}/"
    if kind == "path_absent":
        return f"Keeps {inv.get('path')}/ removed"
    return kind or "invariant"
