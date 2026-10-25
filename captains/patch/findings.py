"""Findings as rows (``PATCH#FINDING#<key>`` in the items table) with their state
(docs/specs/patching-captain.md §3.3, §3.7)::

    open ──(policy: pr)──▶ pr_open ──(merge)──▶ merged ──(the map shows the fix running)──▶ deployed
      │  (policy: inbox) ──▶ approved ──▶ pr_open ...
      └──(a person, with a reason, until a date)──▶ ignored
    any state ──(the inventory no longer matches)──▶ closed, or deployed when it was merged

``sync`` is the one writer the match goes through: new findings open, known ones are
refreshed (the verdict is re-decided as KEV, EPSS and exposure change), findings the
inventory no longer produces close themselves. The pull request states are set by the
fix pipeline; the sync never moves a finding backwards out of them.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional

from boto3.dynamodb.conditions import Key

from captains.patch.match import Finding
from captains.patch.policy import decide, ignored_until
from compass_core.keys import patch_finding_prefix, patch_finding_sort_key

STATES = ("open", "approved", "pr_open", "merged", "deployed", "ignored", "closed")
PR_STATES = ("pr_open", "merged")
OPEN_STATES = ("open", "approved", "pr_open", "merged")


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    return value


def _now_ms() -> int:
    return int(time.time() * 1000)


class FindingStore:
    def __init__(self, table: Any, pk: str):
        self.table = table
        self.pk = pk

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.table.get_item(Key={"pk": self.pk, "sk": patch_finding_sort_key(key)}).get("Item")
        return _to_native({k: v for k, v in row.items() if k not in ("pk", "sk")}) if row else None

    def list(self, *, states: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        wanted = set(states) if states else None
        out: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").begins_with(patch_finding_prefix())}
        while True:
            page = self.table.query(**kwargs)
            for row in page.get("Items", []):
                if wanted is None or str(row.get("state")) in wanted:
                    out.append(_to_native({k: v for k, v in row.items() if k not in ("pk", "sk")}))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(out, key=lambda r: (r.get("state") not in OPEN_STATES, not r.get("exposed"), -(r.get("cvss") or 0), str(r.get("advisory_id")), str(r.get("node_id"))))

    def put(self, row: Dict[str, Any]) -> Dict[str, Any]:
        self.table.put_item(Item={"pk": self.pk, "sk": patch_finding_sort_key(str(row["key"])), **_to_dynamo(row)})
        return row

    def update(self, key: str, **fields: Any) -> Optional[Dict[str, Any]]:
        row = self.get(key)
        if row is None:
            return None
        row.update({k: v for k, v in fields.items()})
        row["updated_at"] = _now_ms()
        return self.put(row)

    # ── transitions a person or the pipeline makes ──
    def approve(self, key: str, *, by: str) -> Optional[Dict[str, Any]]:
        row = self.get(key)
        if row is None or row.get("state") not in ("open", "ignored"):
            return row
        return self.update(key, state="approved", approved_by=by, approved_at=_now_ms(), verdict="pr", verdict_reasons=list(row.get("verdict_reasons") or []) + [f"approved by {by}"])

    def ignore(self, key: str, *, by: str, until: Optional[str], reason: Optional[str]) -> Optional[Dict[str, Any]]:
        row = self.get(key)
        if row is None or row.get("state") in PR_STATES:
            return row
        return self.update(key, state="ignored", ignored={"by": by, "until": until, "reason": reason, "at": _now_ms()})

    def set_pr(self, key: str, *, state: str, pr: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        return self.update(key, state=state, pr=dict(pr))

    # ── the sync ──
    def sync(self, findings: Iterable[Finding], config: Mapping[str, Any], *, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """Write what the match found: open the new, refresh the known, close the gone."""
        now = now_ms or _now_ms()
        existing = {str(r["key"]): r for r in self.list()}
        seen: set = set()
        opened = refreshed = closed = deployed = reopened = 0
        for finding in findings:
            seen.add(finding.key)
            verdict, reasons = decide(finding, config)
            silenced = ignored_until(config, finding.advisory_id)
            row = existing.get(finding.key)
            fresh = finding.to_dict()
            if row is None:
                state = "ignored" if silenced else "open"
                self.put({**fresh, "state": state, "verdict": verdict, "verdict_reasons": reasons, "first_seen": now, "last_seen": now, "updated_at": now,
                          **({"ignored": {"by": "config", "until": silenced["until"], "reason": silenced["reason"], "at": now}} if silenced else {})})
                opened += 1
                continue
            state = str(row.get("state") or "open")
            if state in ("closed", "deployed"):
                state = "open"
                reopened += 1
            elif state == "open" and silenced:
                state = "ignored"
            elif state == "ignored":
                until = (row.get("ignored") or {}).get("until")
                if not silenced and until and str(until)[:10] < time.strftime("%Y-%m-%d", time.gmtime(now / 1000)):
                    state = "open"   # the ignore ran out
            keep = {k: row[k] for k in ("first_seen", "pr", "approved_by", "approved_at", "ignored") if k in row}
            if state in ("approved",):
                verdict = "pr"
            self.put({**row, **fresh, **keep, "state": state, "verdict": verdict if state not in PR_STATES else row.get("verdict", verdict),
                      "verdict_reasons": reasons, "last_seen": now, "updated_at": now})
            refreshed += 1
        for key, row in existing.items():
            if key in seen:
                continue
            state = str(row.get("state") or "open")
            if state in ("closed", "deployed"):
                continue
            if state == "pr_open":
                continue   # the fix is on its way; the watcher moves it on merge
            new_state = "deployed" if state == "merged" else "closed"
            self.put({**row, "state": new_state, f"{new_state}_at": now, "updated_at": now})
            if new_state == "deployed":
                deployed += 1
            else:
                closed += 1
        return {"opened": opened, "refreshed": refreshed, "reopened": reopened, "closed": closed, "deployed": deployed, "total": len(seen)}


def summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    open_rows = [r for r in rows if r.get("state") in OPEN_STATES]
    return {
        "open": len(open_rows), "exposed": sum(1 for r in open_rows if r.get("exposed")), "exploited": sum(1 for r in open_rows if r.get("kev")),
        "pr_now": sum(1 for r in open_rows if r.get("verdict") == "pr" and r.get("state") == "open"),
        "waiting_approval": sum(1 for r in open_rows if r.get("verdict") == "inbox" and r.get("state") == "open"),
        "approved": sum(1 for r in open_rows if r.get("state") == "approved"),
        "pr_open": sum(1 for r in rows if r.get("state") == "pr_open"), "merged": sum(1 for r in rows if r.get("state") == "merged"),
        "deployed": sum(1 for r in rows if r.get("state") == "deployed"), "ignored": sum(1 for r in rows if r.get("state") == "ignored"),
        "closed": sum(1 for r in rows if r.get("state") == "closed"),
        "by_repo": _count(open_rows, "repo"), "by_advisory": _count(open_rows, "advisory_id"),
    }


def _count(rows: Iterable[Dict[str, Any]], field: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if value:
            out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
