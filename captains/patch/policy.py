"""The two outcomes (docs/specs/patching-captain.md §3.4): a pull request without a click when
the vulnerability is exploited (KEV), likely to be (EPSS at or above the threshold), severe
(CVSS at or above the threshold, or a high or critical label when no score is known) or the
workload faces the internet; an inbox item otherwise. A finding whose fix is not a plain bump
(no fix yet, a floating tag, a transitive dependency, a major upgrade) waits in the inbox
whatever its score, with the reason said. ``ignore`` rows silence an advisory until a date.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from captains.patch.match import Finding

DEFAULTS = {"kev": True, "epss_min": 0.1, "cvss_min": 7.0, "exposed": True}
AUTONOMOUS_BLOCKERS_ALLOWED: frozenset = frozenset()   # every blocker sends the finding to the inbox


def settings(config: Mapping[str, Any]) -> Dict[str, Any]:
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    act_on = section.get("act_on") if isinstance(section.get("act_on"), dict) else {}
    return {**DEFAULTS, **{k: v for k, v in act_on.items() if k in DEFAULTS}}


def ignored_until(config: Mapping[str, Any], advisory_id: str, *, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """The ``patch.ignore`` entry that silences this advisory today, if any."""
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    today = today or datetime.now(timezone.utc).date()
    for entry in section.get("ignore") or []:
        if not isinstance(entry, dict) or str(entry.get("advisory") or "").upper() != advisory_id.upper():
            continue
        until = str(entry.get("until") or "")
        try:
            if until and date.fromisoformat(until[:10]) < today:
                continue
        except ValueError:
            pass
        return {"until": until or None, "reason": entry.get("reason")}
    return None


def decide(finding: Finding, config: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """``("pr" | "inbox", reasons)``: why the pull request opens on its own, or why it waits."""
    rules = settings(config)
    reasons: List[str] = []
    if rules["kev"] and finding.kev:
        reasons.append("exploited in the wild (KEV)")
    if finding.epss is not None and finding.epss >= float(rules["epss_min"]):
        reasons.append(f"EPSS {finding.epss:.2f} ≥ {float(rules['epss_min']):.2f}")
    if finding.cvss is not None and finding.cvss >= float(rules["cvss_min"]):
        reasons.append(f"CVSS {finding.cvss} ≥ {float(rules['cvss_min'])}")
    elif finding.cvss is None and (finding.severity or "") in ("high", "critical"):
        reasons.append(f"severity {finding.severity} (no score published)")
    if rules["exposed"] and finding.exposed:
        reasons.append("the workload faces the internet")
    if not reasons:
        return "inbox", ["below every trigger: not exploited, not exposed, not severe"]
    blocked = [b for b in finding.blockers if b not in AUTONOMOUS_BLOCKERS_ALLOWED]
    if blocked:
        return "inbox", reasons + [f"waits: {_blocker_text(b)}" for b in blocked]
    return "pr", reasons


def _blocker_text(blocker: str) -> str:
    return {
        "no_fix": "no fixed version yet",
        "floating_tag": "a floating image tag; the fix is a rebuild on a pinned tag",
        "transitive": "a transitive dependency; the direct dependency that brings it must move",
        "major_upgrade": "the only fix is a major upgrade",
        "unknown_version": "the running version is not known exactly",
        "dev_only": "a development dependency; it does not run in production",
    }.get(blocker, blocker.replace("_", " "))
