from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ScoreBreakdown = Tuple[int, List[str]]

# -- Utility helpers ---------------------------------------------------------------------------

def clamp_score(value: float, minimum: int = 1, maximum: int = 5) -> int:
    """Clamp floating values to the inclusive score bounds."""
    try:
        numeric = float(value)
    except Exception:
        numeric = minimum
    return max(minimum, min(int(round(numeric)), maximum))


def parse_llm_urgency(raw: Any, default: int = 3) -> int:
    """Coerce common LLM outputs (JSON blob or plain number) to an urgency score."""
    if raw is None:
        return default
    try:
        if isinstance(raw, (int, float)):
            return clamp_score(raw)
        if isinstance(raw, str):
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                return clamp_score(int(digits))
    except Exception:
        pass
    return default


def keyword_hits(text: str, keyword_map: Dict[str, int]) -> List[Tuple[str, int]]:
    """Return matched keywords with their configured boosts (case-insensitive contains)."""
    if not isinstance(text, str) or not keyword_map:
        return []
    lowered = text.lower()
    hits: List[Tuple[str, int]] = []
    for needle, boost in keyword_map.items():
        if not needle:
            continue
        if needle.lower() in lowered:
            try:
                hits.append((needle, int(boost)))
            except Exception:
                continue
    return hits


def compute_urgency_score(
    base_score: int,
    *,
    source: Optional[str],
    deadline: Optional[datetime],
    keyword_boosts: Sequence[Tuple[str, int]],
    cfg: Dict[str, Any],
    now: Optional[datetime] = None,
) -> ScoreBreakdown:
    """Apply deterministic boosts on top of LLM-provided urgency score."""
    score = clamp_score(base_score)
    notes: List[str] = []

    urgency_cfg = cfg.get("urgency", {}) if isinstance(cfg, dict) else {}
    source_boosts = urgency_cfg.get("source_boosts", {})
    deadline_cfg = urgency_cfg.get("deadline", {})

    if source and isinstance(source_boosts, dict):
        adjusted_source = source_boosts.get(source) or source_boosts.get(source.lower())
        if adjusted_source:
            try:
                delta = int(adjusted_source)
                score += delta
                notes.append(f"source={source}:+{delta}")
            except Exception:
                pass

    if deadline:
        ref = now or datetime.now(timezone.utc)
        if deadline.tzinfo is None:
            # Assume UTC if not provided
            deadline = deadline.replace(tzinfo=timezone.utc)
        mins = (deadline - ref).total_seconds() / 60
        if mins < 0:
            mins = 0
        threshold_cfg = deadline_cfg.get("thresholds_min", [60, 240, 1440])
        boosts_cfg = deadline_cfg.get("boosts", [2, 1, 0])
        try:
            thresholds = [float(x) for x in threshold_cfg]
        except Exception:
            thresholds = [60, 240, 1440]
        applied = 0
        for idx, threshold in enumerate(thresholds):
            if mins <= threshold:
                try:
                    delta = int(boosts_cfg[idx])
                except Exception:
                    delta = applied
                score += delta
                applied = delta
                notes.append(f"deadline<={int(threshold)}m:+{delta}")
                break

    for keyword, boost in keyword_boosts:
        if boost:
            score += boost
            notes.append(f"keyword={keyword}:+{boost}")

    score = clamp_score(score)
    return score, notes


# -- Risk scoring -------------------------------------------------------------------------------

WEIGHTS: Dict[str, float] = {
    "blast_radius": 0.30,
    "data_loss": 0.25,
    "reversibility": 0.20,
    "operational_load": 0.20,
    "compliance": 0.05,
}

CRITICAL_RESOURCE_TYPES = {"rds", "aurora", "aurora.cluster", "ec2", "elb", "elbv2", "lambda"}


def _score_blast_radius(resources: Sequence[Dict[str, Any]]) -> float:
    if not resources:
        return 0.2
    base = 0.3
    unique_types = {r.get("type") for r in resources if isinstance(r, dict)}
    if len(resources) > 1:
        base += 0.2
    if any((t or "").lower() in CRITICAL_RESOURCE_TYPES for t in unique_types if t):
        base += 0.3
    envs = {((r.get("env") or r.get("environment") or "").lower()) for r in resources if isinstance(r, dict)}
    if any(e in {"prod", "production"} for e in envs):
        base += 0.2
    return min(base, 1.0)


def _score_data_loss(evidence: Dict[str, Any], change_type: Optional[str]) -> float:
    base = 0.2
    if change_type:
        ct = change_type.upper()
        if "DELETE" in ct or "RETENTION" in ct or "BACKUP" in ct:
            base += 0.4
        if "DOWNSIZE" in ct:
            base += 0.3
    metrics = evidence.get("metrics") if isinstance(evidence, dict) else []
    if metrics:
        for metric in metrics:
            name = (metric or {}).get("name", "").lower()
            if name in {"freestoragespace", "freeablememory"}:
                try:
                    value = float(metric.get("value"))
                    if value < 10:
                        base += 0.2
                except Exception:
                    continue
    return min(base, 1.0)


def _score_reversibility(change_type: Optional[str]) -> float:
    if not change_type:
        return 0.5
    ct = change_type.upper()
    if any(term in ct for term in ["DELETE", "DROP", "DESTROY"]):
        return 0.9
    if any(term in ct for term in ["DOWNSIZE", "SCALE_DOWN", "REDUCE"]):
        return 0.7
    if any(term in ct for term in ["ENABLE", "CREATE", "INCREASE"]):
        return 0.6
    return 0.5


def _score_operational_load(evidence: Dict[str, Any]) -> float:
    metrics = evidence.get("metrics") if isinstance(evidence, dict) else []
    if not metrics:
        return 0.4
    high_load_metrics = 0
    for metric in metrics:
        name = (metric or {}).get("name", "").lower()
        try:
            value = float(metric.get("value"))
        except Exception:
            value = 0.0
        if name in {"cpuutilization", "databaseconnections", "targetresponsetime", "httpcode_target_5xx_count"}:
            if value >= 80:
                high_load_metrics += 1
    if high_load_metrics >= 2:
        return 0.9
    if high_load_metrics == 1:
        return 0.7
    return 0.4


def _score_compliance(resources: Sequence[Dict[str, Any]], evidence: Dict[str, Any]) -> float:
    tags = []
    for res in resources:
        if isinstance(res, dict):
            tags.extend((res.get("tags") or {}).keys())
    if any("pci" in t.lower() or "sox" in t.lower() for t in tags):
        return 0.9
    assumptions = evidence.get("assumptions") if isinstance(evidence, dict) else []
    if any("compliance" in (a or "").lower() for a in assumptions):
        return 0.7
    return 0.3


def compute_risk_score(
    resources: Sequence[Dict[str, Any]],
    evidence: Dict[str, Any],
    *,
    change_type: Optional[str],
    calibration_cfg: Dict[str, int],
    env: Optional[str] = None,
) -> Tuple[int, Dict[str, float], List[str]]:
    """Return (risk_score, component_breakdown, calibration_notes)."""
    breakdown = {
        "blast_radius": _score_blast_radius(resources),
        "data_loss": _score_data_loss(evidence, change_type),
        "reversibility": _score_reversibility(change_type),
        "operational_load": _score_operational_load(evidence),
        "compliance": _score_compliance(resources, evidence),
    }

    weighted = sum(breakdown[name] * weight for name, weight in WEIGHTS.items())
    scaled = clamp_score(weighted * 5)

    calibration_notes: List[str] = []
    if calibration_cfg and isinstance(calibration_cfg, dict):
        if change_type:
            delta = calibration_cfg.get(change_type.upper())
            if delta:
                scaled = clamp_score(scaled + delta)
                calibration_notes.append(f"change_type={change_type}:{delta:+d}")
    if env:
        env_upper = env.upper()
        env_delta = calibration_cfg.get(f"ENV::{env_upper}") if calibration_cfg else None
        if env_delta:
            scaled = clamp_score(scaled + env_delta)
            calibration_notes.append(f"env={env}:{env_delta:+d}")

    return scaled, breakdown, calibration_notes


def calculate_confidence(successful: int, planned: int) -> float:
    if planned <= 0:
        return 0.0
    try:
        return max(0.0, min(1.0, successful / float(planned)))
    except Exception:
        return 0.0


# ── The mode/risk gate ──────────────────────────────────────────────────────
# Shared by the decoys' response actions and the PR watcher's revisions. The
# risk comes from compute_risk_score above; the LLM never sets it.

MODES = ("off", "approve", "autopilot")


def gate(mode: str, risk: int, risk_max: int) -> Dict[str, Any]:
    """``run`` (autopilot and within risk_max), ``wait_for_approval``, or
    ``recommend_only``. Never consults urgency."""
    mode = (mode or "off").lower()
    if mode not in MODES:
        return {"decision": "recommend_only", "reason": f"unknown mode {mode!r}"}
    if mode == "off":
        return {"decision": "recommend_only", "reason": "action is enabled for recommendation only"}
    if mode == "approve":
        return {"decision": "wait_for_approval", "reason": "mode is approve"}
    if int(risk) <= int(risk_max):
        return {"decision": "run", "reason": f"autopilot: risk {risk} <= risk_max {risk_max}"}
    return {"decision": "wait_for_approval", "reason": f"autopilot: risk {risk} > risk_max {risk_max}"}
