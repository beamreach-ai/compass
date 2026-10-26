"""The patch captain's inbox items (docs/specs/patching-captain.md §3.8): the findings the policy
sent to the inbox, one item per repository (or per workload when no repository builds it),
with "approve" opening the pull request the autonomous path would have opened. ``items`` is
what the manifest names."""

from __future__ import annotations

from typing import Any, Dict, List

from captains.patch.findings import FindingStore
from compass_core.inbox import Action, Context, Item


def items(ctx: Context) -> List[Item]:
    if ctx.table is None:
        return []
    store = FindingStore(ctx.table, ctx.pk)
    waiting = [r for r in store.list(states=["open"]) if r.get("verdict") == "inbox"]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in waiting:
        groups.setdefault(str(row.get("repo") or f"workload:{row.get('node_name')}"), []).append(row)
    out: List[Item] = []
    for group, rows in groups.items():
        rows.sort(key=lambda r: (-(r.get("cvss") or 0), str(r.get("advisory_id"))))
        repo = None if group.startswith("workload:") else group
        where = repo or group.split(":", 1)[1]
        exposed = any(r.get("exposed") for r in rows)
        kev = any(r.get("kev") for r in rows)
        advisories = sorted({str(r.get("advisory_id")) for r in rows})
        keys = [str(r["key"]) for r in rows]
        count = len(rows)
        body = {"keys": keys}
        out.append(Item(
            id=f"patch.approve:{group}", kind="patch.approve", verb="approve", severity="urgent" if kev or exposed else "normal", captain="patch", tab="patches",
            title=f"Open the pull request for {count} finding{'s' if count != 1 else ''} in {where}" if repo else f"Review {count} finding{'s' if count != 1 else ''} on {where}",
            summary=" · ".join(filter(None, [", ".join(advisories[:3]) + (f" +{len(advisories) - 3}" if len(advisories) > 3 else ""),
                                              "exposed" if exposed else None, "exploited" if kev else None,
                                              (rows[0].get("verdict_reasons") or [""])[-1]])),
            since=min(int(r.get("first_seen") or 0) for r in rows), subject={"repo": repo, "keys": keys, "advisories": advisories},
            actions=[a for a in (
                Action(key="approve", label="Approve the pull request" if repo else "Approve", method="POST", path="/api/patches/findings/approve", body=body, scope="operate",
                       confirm=f"Marks {count} finding{'s' if count != 1 else ''} approved; the next tick opens the fix pull request{' in ' + repo if repo else ''}."),
                Action(key="ignore", label="Ignore for 30 days", method="POST", path="/api/patches/findings/ignore", body={**body, "days": 30}, scope="operate"),
                Action(key="open", label="Open patches", href=f"/workspace?tab=patches&view=needs_you{'&repo=' + repo if repo else ''}"),
            ) if a is not None],
            detail={"findings": [{k: r.get(k) for k in ("advisory_id", "package", "version", "fixed_version", "node_name", "path", "exposed", "kev", "epss", "cvss",
                                                         "blockers", "note", "verdict_reasons", "summary")} for r in rows]},
        ))
    return out
