"""The PR watcher's inbox items (docs/specs/one-inbox.md §3.3): a revision waiting for
approval, and a pull request the watcher gave up on. ``items`` is what the manifest names."""

from __future__ import annotations

from typing import Any, List, Mapping

from captains.prs.captain import _REASON_TEXT
from compass_core.inbox import Action, Context, Item, _ms, _plural, _short


def _ref(record: Mapping[str, Any]) -> str:
    return f"{record.get('repo')}#{record.get('number')}"


def _path(record: Mapping[str, Any], tail: str) -> str:
    return f"/api/prs/{record.get('repo')}/{record.get('number')}/{tail}"


def items(ctx: Context) -> List[Item]:
    out: List[Item] = []
    for record in ctx.pr_records():
        if str(record.get("state") or "open") != "open":
            continue
        ref = _ref(record)
        pending = record.get("pending_revision") if isinstance(record.get("pending_revision"), dict) else None
        watch = record.get("watch") if isinstance(record.get("watch"), dict) else {}
        subject = {"repo": record.get("repo"), "number": record.get("number")}
        if pending:
            feedback = pending.get("feedback") or []
            out.append(Item(
                id=f"pr.revision:{ref}", kind="pr.revision", verb="approve", severity="normal", captain="prs", tab="prs",
                title=f"Approve the revision to {ref}",
                summary=_short(pending.get("summary") or pending.get("plan") or f"{_plural(len(feedback), 'review comment')} to address"),
                since=_ms(pending.get("created_at") or record.get("updated_at")), subject=subject,
                actions=[
                    Action(key="approve", label="Approve the revision", method="POST", path=_path(record, "revision/approve"), scope="operate",
                           confirm=f"Pushes the planned revision to {ref}."),
                    Action(key="dismiss", label="Dismiss", method="POST", path=_path(record, "revision/dismiss"), scope="operate"),
                    Action(key="open", label="Open on GitHub", href=record.get("url")),
                ],
                detail={"comments": feedback, "files": pending.get("files") or pending.get("allowed_files"),
                        "plan": pending.get("plan") or pending.get("summary"), "risk": pending.get("risk")},
            ))
        if str(watch.get("status") or "") == "needs_human":
            reason = str(watch.get("reason") or "")
            out.append(Item(
                id=f"pr.needs_human:{ref}", kind="pr.needs_human", verb="fix", severity="normal", captain="prs", tab="prs",
                title=f"Compass needs you on {ref}",
                summary=_REASON_TEXT.get(reason) or reason.replace("_", " ") or "the watcher stopped",
                since=_ms(watch.get("since") or record.get("updated_at")), subject=subject,
                actions=[
                    Action(key="open", label="Open on GitHub", href=record.get("url")),
                    Action(key="resume", label="Resume", method="POST", path=_path(record, "resume"), scope="operate"),
                    Action(key="pause", label="Pause", method="POST", path=_path(record, "pause"), scope="operate"),
                ],
                detail={"reason": reason, "reason_text": _REASON_TEXT.get(reason), "title": record.get("title"), "branch": record.get("branch"),
                        "last_decision": (record.get("decisions") or [None])[-1]},
            ))
    return out
