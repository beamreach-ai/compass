"""The honeypot gate: score a response action and decide whether it runs.

Deterministic. ``compute_risk_score`` sets the risk from the action's
declared change type and the one resource it touches; the LLM never sets it.
Unlike the FinOps control flags, urgency is not a cap here: a honeytoken trip
is the high-confidence signal, so high urgency is exactly when acting is right.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from captains.honeypot.actions.base import ActionContext, ResponseAction
from compass_core.policy import MODES, compute_risk_score, gate  # noqa: F401 - gate and MODES are re-exported

__all__ = ["DEFAULT_CALIBRATION", "MODES", "gate", "score_action"]

# Reversibility and data loss are known for catalog actions, which the generic
# keyword scorer cannot see; these deltas state them explicitly.
DEFAULT_CALIBRATION: Dict[str, int] = {
    "ECS_TASK_RESTART": 0,
    "IAM_KEY_DEACTIVATE": -1,
}


def score_action(
    action: ResponseAction,
    ctx: ActionContext,
    snapshot: Dict[str, Any],
    *,
    calibration_cfg: Optional[Dict[str, int]] = None,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    resources, evidence = action.risk_inputs(ctx, snapshot)
    calibration = dict(DEFAULT_CALIBRATION)
    if calibration_cfg:
        calibration.update({str(k).upper(): int(v) for k, v in calibration_cfg.items()})
    risk, breakdown, notes = compute_risk_score(
        resources,
        evidence,
        change_type=action.change_type,
        calibration_cfg=calibration,
        env=env or ctx.environment or None,
    )
    return {
        "risk": int(risk),
        "breakdown": {k: round(float(v), 3) for k, v in breakdown.items()},
        "notes": list(notes),
        "change_type": action.change_type,
        "rollback_supported": action.rollback_supported,
    }
