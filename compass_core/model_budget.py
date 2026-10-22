"""How many model calls a workspace may make: per hour and per day, counted in its items table.

Every call through ``compass_core.llm`` claims one unit first; a model the product builds
itself (its Bedrock client) claims through ``claim`` too and says so with ``budgeted = True``,
so a call is never counted twice. A claim is one conditional ``ADD`` per window, so two
processes racing for the last unit get one between them; past the limit the call is refused
with ``ModelBudgetExceeded`` before it reaches the provider.

The limits are ``limits.model_calls_per_hour`` and ``limits.model_calls_per_day`` in the
workspace config. Outside demo mode an unset limit is no limit, as before; in demo mode,
where strangers press the buttons, the defaults are ``DEMO_DEFAULTS``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from compass_core.keys import model_calls_sort_key, workspace_pk

logger = logging.getLogger("compass.model_budget")

DEMO_DEFAULTS = {"hour": 60, "day": 300}
_KEYS = {"hour": "model_calls_per_hour", "day": "model_calls_per_day"}
_TTL = {"hour": 2 * 3600, "day": 2 * 86400}


class ModelBudgetExceeded(RuntimeError):
    """The workspace has used its model calls for this hour or day."""

    def __init__(self, window: str, limit: int, used: int) -> None:
        super().__init__(f"the workspace's model budget is used up: {used} of {limit} calls this {window}")
        self.window, self.limit, self.used = window, limit, used


def limits(config: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """``{"hour": n | None, "day": n | None}``: the config's numbers, else the demo's, else none."""
    from compass_core import demo

    block = (config or {}).get("limits") if isinstance(config, dict) else None
    block = block if isinstance(block, dict) else {}
    out: Dict[str, Optional[int]] = {}
    for window, key in _KEYS.items():
        raw = block.get(key)
        value: Optional[int] = None
        if raw is not None and str(raw).strip() != "":
            try:
                value = max(0, int(raw))
            except (TypeError, ValueError):
                value = None
        if value is None and demo.enabled():
            value = DEMO_DEFAULTS[window]
        out[window] = value
    return out


def _window_id(window: str, now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H") if window == "hour" else now.strftime("%Y-%m-%d")


def _table(config: Dict[str, Any]) -> Any:
    from compass_core.tables import dynamo_table, tables

    name = tables(config).items
    return dynamo_table(name) if name else None


def claim(config: Optional[Dict[str, Any]], *, workspace_id: Optional[str] = None, units: int = 1,
          table: Any = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Take ``units`` calls from this hour's and this day's budget, or raise ``ModelBudgetExceeded``.

    A window without a limit is not counted. A day refused after the hour was taken gives the
    hour back. Without an items table there is nothing to count in: the claim passes, logged,
    unless demo mode requires a budget."""
    config = config or {}
    workspace_id = workspace_id or str(config.get("workspace_id") or "")
    wanted = {w: n for w, n in limits(config).items() if n is not None}
    if not wanted:
        return {"claimed": 0, "limits": {}}
    table = table if table is not None else _table(config)
    if table is None or not workspace_id:
        from compass_core import demo

        if demo.enabled():
            raise ModelBudgetExceeded("hour", 0, 0)
        logger.warning("model budget: no items table or workspace id; the call is not counted")
        return {"claimed": 0, "limits": wanted}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pk = workspace_pk(workspace_id)
    taken = []
    try:
        for window in ("hour", "day"):
            if window not in wanted:
                continue
            limit = int(wanted[window])
            key = {"pk": pk, "sk": model_calls_sort_key(window, _window_id(window, now))}
            if limit < units:  # 0 is "no model calls at all"; the condition below would let the first through
                raise ModelBudgetExceeded(window, limit, _used(table, key))
            try:
                table.update_item(
                    Key=key,
                    UpdateExpression="ADD calls :n SET expires_at = if_not_exists(expires_at, :ttl)",
                    ConditionExpression="attribute_not_exists(calls) OR calls <= :room",
                    ExpressionAttributeValues={":n": units, ":room": limit - units, ":ttl": int(time.time()) + _TTL[window]},
                )
            except Exception as exc:  # noqa: BLE001 - the condition, or the table
                if type(exc).__name__ == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in str(exc):
                    used = _used(table, key)
                    raise ModelBudgetExceeded(window, limit, used) from None
                raise
            taken.append(key)
    except ModelBudgetExceeded:
        for key in taken:  # the hour back when the day refused
            try:
                table.update_item(Key=key, UpdateExpression="ADD calls :n", ExpressionAttributeValues={":n": -units})
            except Exception:  # noqa: BLE001
                logger.warning("model budget: could not give back %s", key["sk"])
        raise
    return {"claimed": units, "limits": wanted}


def _used(table: Any, key: Dict[str, str]) -> int:
    try:
        item = table.get_item(Key=key).get("Item") or {}
        return int(item.get("calls") or 0)
    except Exception:  # noqa: BLE001
        return 0


def usage(config: Optional[Dict[str, Any]], *, workspace_id: Optional[str] = None, table: Any = None,
          now: Optional[datetime] = None) -> Dict[str, Dict[str, Optional[int]]]:
    """This hour's and today's calls against their limits, for status and the settings page."""
    config = config or {}
    workspace_id = workspace_id or str(config.get("workspace_id") or "")
    caps = limits(config)
    table = table if table is not None else (_table(config) if workspace_id else None)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out: Dict[str, Dict[str, Optional[int]]] = {}
    for window in ("hour", "day"):
        used = 0
        if table is not None and workspace_id:
            used = _used(table, {"pk": workspace_pk(workspace_id), "sk": model_calls_sort_key(window, _window_id(window, now))})
        out[window] = {"used": used, "limit": caps[window]}
    return out
