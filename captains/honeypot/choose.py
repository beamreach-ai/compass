"""HP-10: the agent chooses *which* whitelisted actions to run.

The choice is constrained three times: only rows the user enabled are
offered, only rows whose scope matches the incident's target are applicable,
and whatever the model answers is validated against that set. Without a
model (or when it answers nonsense) the deterministic plan is every
applicable row in catalog order, which is what the demo whitelist produces
anyway.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from captains.honeypot.actions import ACTION_CATALOG, get_action
from captains.honeypot.actions.base import ActionContext, ActionScope, ScopeError

logger = logging.getLogger("honeypot-choose")

PROMPT_PATH = Path(__file__).parent / "prompts" / "choose_actions.md"
CATALOG_ORDER = list(ACTION_CATALOG)


def applicable_rows(rows: List[Dict[str, Any]], ctx: ActionContext) -> List[Dict[str, Any]]:
    """Enabled rows whose scope covers the incident's target, in catalog order."""
    out: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: CATALOG_ORDER.index(r["action_type"]) if r.get("action_type") in CATALOG_ORDER else 99):
        if not row.get("enabled", True) or row.get("action_type") not in ACTION_CATALOG:
            continue
        if (row.get("mode") or "off") == "off":
            continue
        try:
            get_action(row["action_type"]).check_scope(ActionScope.from_dict(row.get("scope")), ctx)
        except ScopeError:
            continue
        out.append(row)
    return out


def deterministic_plan(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "action_ids": [r["action_id"] for r in rows],
        "rationale": "every applicable whitelisted action, in catalog order",
        "chosen_by": "policy",
    }


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _parse(text: Any) -> Dict[str, Any]:
    if isinstance(text, dict):
        return text
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    return json.loads(raw)


def choose_actions(
    facts: Dict[str, Any],
    rows: List[Dict[str, Any]],
    ctx: ActionContext,
    *,
    invoke: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    """Return ``{action_ids, rationale, chosen_by}``; ``action_ids`` ⊆ applicable rows.

    ``invoke(system_prompt, user_json) -> text``. When None and
    HONEYPOT_AGENT_CHOICE is not "llm", the plan is deterministic.
    """
    candidates = applicable_rows(rows, ctx)
    if not candidates:
        return {"action_ids": [], "rationale": "no enabled action covers this target", "chosen_by": "policy"}
    use_llm = invoke is not None or os.getenv("HONEYPOT_AGENT_CHOICE", "").lower() == "llm"
    if not use_llm:
        return deterministic_plan(candidates)

    offered = [
        {
            "action_id": r["action_id"],
            "action_type": r["action_type"],
            "title": get_action(r["action_type"]).title,
            "description": get_action(r["action_type"]).description,
            "mode": r.get("mode"),
            "risk_max": r.get("risk_max"),
        }
        for r in candidates
    ]
    try:
        if invoke is None:
            from captains.honeypot.summary import default_invoke as invoke  # type: ignore[assignment]
        answer = _parse(invoke(_load_prompt(), json.dumps({"incident": facts, "allowed_actions": offered}, indent=2, default=str)))
        ids = [str(a) for a in (answer.get("action_ids") or [])]
        valid = [r["action_id"] for r in candidates]
        chosen = [i for i in ids if i in valid]
        if not chosen or len(chosen) != len(ids):
            logger.warning("model answered with ids outside the allowed set (%s); using the deterministic plan", ids)
            plan = deterministic_plan(candidates)
            plan["rationale"] = f"model answer rejected ({ids}); {plan['rationale']}"
            return plan
        return {"action_ids": chosen, "rationale": str(answer.get("rationale") or ""), "chosen_by": "agent"}
    except Exception as exc:  # noqa: BLE001 - never let the model block containment
        logger.warning("action choice failed (%s); using the deterministic plan", exc)
        plan = deterministic_plan(candidates)
        plan["rationale"] = f"model unavailable ({type(exc).__name__}); {plan['rationale']}"
        return plan
