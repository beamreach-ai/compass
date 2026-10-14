"""Settings and the decision gate for pushing a revision."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from captains.honeypot.policy import gate

MODES = ("off", "approve", "autopilot")
DEFAULTS: Dict[str, Any] = {
    "mode": "autopilot",
    "risk_max": 2,
    "max_revisions": 3,
    "reply": True,
    "nudge_after_days": None,
    "auto_merge": {
        "enabled": False,
        "allow_types": ["iac_import"],
        "repos": [],
        "risk_max": 1,
        "fix_rounds": 0,
        "merge_method": "squash",
        "reviewer": {"model": "mistral-codestral-22b"},
    },
}

_SENSITIVE_PATH_RE = re.compile(r"(^|/)(iam|kms|security[-_]?groups?|network|vpc|payments?|prod(uction)?)(/|\.|$)", re.IGNORECASE)


def watcher_settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = config if isinstance(config, dict) else {}
    prs_cfg = config.get("prs") if isinstance(config.get("prs"), dict) else {}
    raw = prs_cfg.get("watcher") if isinstance(prs_cfg.get("watcher"), dict) else {}
    out = dict(DEFAULTS)
    out["auto_merge"] = dict(DEFAULTS["auto_merge"])
    for key in ("mode", "risk_max", "max_revisions", "reply", "nudge_after_days"):
        if key in raw and raw[key] is not None:
            out[key] = raw[key]
    mode = str(out.get("mode") or "autopilot").lower()
    out["mode"] = mode if mode in MODES else "autopilot"
    try:
        out["risk_max"] = max(0, min(5, int(out["risk_max"])))
    except (TypeError, ValueError):
        out["risk_max"] = DEFAULTS["risk_max"]
    try:
        out["max_revisions"] = max(0, min(10, int(out["max_revisions"])))
    except (TypeError, ValueError):
        out["max_revisions"] = DEFAULTS["max_revisions"]
    out["reply"] = bool(out["reply"])
    am = raw.get("auto_merge") if isinstance(raw.get("auto_merge"), dict) else {}
    for key in ("enabled", "allow_types", "repos", "risk_max", "fix_rounds", "merge_method", "reviewer"):
        if key in am and am[key] is not None:
            out["auto_merge"][key] = am[key]
    out["auto_merge"]["enabled"] = bool(out["auto_merge"]["enabled"])
    return out


def apply_settings_update(config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    prs_cfg = config.setdefault("prs", {})
    if not isinstance(prs_cfg, dict):
        prs_cfg = config["prs"] = {}
    watcher = prs_cfg.setdefault("watcher", {})
    if not isinstance(watcher, dict):
        watcher = prs_cfg["watcher"] = {}
    for key in ("mode", "risk_max", "max_revisions", "reply", "nudge_after_days"):
        if key in payload:
            watcher[key] = payload[key]
    if isinstance(payload.get("auto_merge"), dict):
        am = watcher.setdefault("auto_merge", {})
        if not isinstance(am, dict):
            am = watcher["auto_merge"] = {}
        for key, value in payload["auto_merge"].items():
            am[key] = value
    return watcher_settings(config)


def requested_files_from_feedback(feedback: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for comment in (feedback or {}).get("comments") or []:
        if isinstance(comment, dict) and isinstance(comment.get("path"), str) and comment["path"].strip():
            path = comment["path"].strip()
            if path not in files:
                files.append(path)
    for path in (feedback or {}).get("conflicts") or []:
        if isinstance(path, str) and path.strip() and path.strip() not in files:
            files.append(path.strip())
    return files


def revision_risk(intent: Dict[str, Any], requested_files: List[str]) -> Dict[str, Any]:
    """Deterministic: the finding's risk at creation, +1 outside the original files, +1 on sensitive paths."""
    base = intent.get("risk")
    try:
        risk = int(base) if base is not None else 2
    except (TypeError, ValueError):
        risk = 2
    notes: List[str] = [f"base risk {risk}"]
    scope_files = set((intent.get("scope") or {}).get("files") or [])
    outside = [f for f in requested_files if scope_files and f not in scope_files]
    if outside:
        risk += 1
        notes.append("+1: touches files outside the original change (" + ", ".join(outside[:5]) + ")")
    sensitive = [f for f in requested_files if _SENSITIVE_PATH_RE.search(f)]
    if sensitive:
        risk += 1
        notes.append("+1: sensitive path (" + ", ".join(sensitive[:5]) + ")")
    return {"risk": max(1, min(5, risk)), "notes": notes}


def decide(settings: Dict[str, Any], *, risk: int, record: Dict[str, Any], approved: bool, trigger: str) -> Dict[str, Any]:
    """``run``, ``wait_for_approval``, ``recommend_only`` or ``blocked`` with a reason."""
    if str(os.getenv("READ_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"decision": "blocked", "reason": "read_only"}
    watch = record.get("watch") if isinstance(record.get("watch"), dict) else {}
    status = str(watch.get("status") or "watching")
    if status in ("paused", "done", "needs_human"):
        return {"decision": "blocked", "reason": f"watch_{status}"}
    if str(record.get("state") or "open") != "open":
        return {"decision": "blocked", "reason": "pr_not_open"}
    counters = record.get("counters") if isinstance(record.get("counters"), dict) else {}
    done = int(counters.get("revisions") or 0)
    if trigger != "manual" and done >= int(settings.get("max_revisions") or 0):
        return {"decision": "blocked", "reason": "max_revisions"}
    if approved:
        return {"decision": "run", "reason": "approved by a person"}
    verdict = gate(settings.get("mode") or "off", int(risk), int(settings.get("risk_max") or 0))
    return verdict
