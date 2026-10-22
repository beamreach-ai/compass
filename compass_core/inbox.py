"""The inbox: everything that needs a person, derived from state, as one list
(docs/specs/one-inbox.md).

An item is one thing a person is needed for. It is never stored: each kind is a
rule over state the captains already keep (a parked decoy response, a grant to
accept, a pull request the watcher gave up on, a failed job, an audit finding, an
available update, an incomplete setup), so an item appears when the state says a
person is needed and leaves when the state changes. Its id is deterministic
(``<kind>:<subject>``), its ``actions`` are the routes the tabs already call, and
its ``detail`` is what the tab would show before the same click, as data.

The only hiding is a snooze, for the kinds whose item has a natural end: the row
``INBOX#SNOOZE#<id>`` hides that id, and the id carries the end (a job id, a
release version), so a newer failure or a newer release is a new item.

The core's kinds live here (grants, failed jobs, audit findings, the update, the
setup); a captain's live in its package and its manifest names the function
(``inbox="captains.honeypot.inbox:items"``). A product captain that is not a
distribution of its own registers one through ``register_source`` (the same registry
``compass_api.extensions.register_inbox_source`` fills). A source that raises is
logged and skipped; it never breaks the list.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from boto3.dynamodb.conditions import Key

from compass_core.keys import inbox_snooze_prefix, inbox_snooze_sort_key

logger = logging.getLogger("compass.inbox")

VERBS = ("approve", "fix", "merge", "later")
SEVERITIES = ("urgent", "normal", "info")
CACHE_SECONDS = 5
JOBS_WINDOW = 50
_FAILED_JOB_STATUSES = frozenset({"failed", "dead", "error"})
_URGENT_FINDING_KINDS = frozenset({"unexplained"})


@dataclass
class Action:
    key: str
    label: str
    method: Optional[str] = None      # None for a link
    path: Optional[str] = None
    body: Optional[Dict[str, Any]] = None
    scope: Optional[str] = None       # the token scope the route needs
    confirm: Optional[str] = None     # the sentence shown before a second click
    href: Optional[str] = None
    snooze: bool = False              # the action is "snooze this item" (POST /api/inbox/{id}/snooze)


@dataclass
class Item:
    id: str
    kind: str
    verb: str
    severity: str
    captain: str
    tab: str
    title: str
    summary: str
    since: int                        # epoch ms when the state began; the sort key
    subject: Dict[str, Any] = field(default_factory=dict)
    actions: List[Action] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    snooze: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out = _native(asdict(self))
        out["actions"] = [{k: v for k, v in a.items() if v not in (None, False)} for a in out["actions"]]
        return out


@dataclass
class Context:
    """What the kinds read. ``table`` is the items table (None on an install without one);
    the callables fetch what lives elsewhere, so a route or a test can hand in its own."""

    config: Dict[str, Any]
    workspace_id: str
    pk: str
    table: Any = None
    jobs: Callable[[], List[Dict[str, Any]]] = lambda: []
    pr_records: Callable[[], List[Dict[str, Any]]] = lambda: []
    update: Callable[[], Dict[str, Any]] = lambda: {}
    model_available: Callable[[], bool] = lambda: False
    now_ms: Optional[int] = None

    def now(self) -> int:
        return int(self.now_ms if self.now_ms is not None else time.time() * 1000)


Source = Callable[[Context], Iterable[Item]]
_sources: Dict[str, Source] = {}
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, int, List[Item]]] = {}   # workspace -> (expires, generated_at, items)


def register_source(name: str, fn: Optional[Source]) -> None:
    """A product captain's items, in the same shape; ``None`` removes it."""
    with _lock:
        if fn is None:
            _sources.pop(name, None)
        else:
            _sources[name] = fn


def reset_for_tests() -> None:
    with _lock:
        _sources.clear()
        _cache.clear()


# ── helpers ──────────────────────────────────────────────────────────────────

def _native(value: Any) -> Any:
    """DynamoDB's Decimals as ints or floats, so an item is JSON as it stands."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(v) for v in value]
    if isinstance(value, set):
        return sorted(_native(v) for v in value)
    return value


def _ms(value: Any) -> int:
    """An epoch ms from an epoch (s or ms), an ISO-8601 string, or nothing (0)."""
    if value in (None, "", 0):
        return 0
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        number = float(value)
        return int(number if number > 1e11 else number * 1000)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return 0


def _query(table: Any, pk: str, prefix: str) -> List[Dict[str, Any]]:
    if table is None:
        return []
    out: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix)}
    while True:
        page = table.query(**kwargs)
        out.extend(dict(i) for i in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return out


def _short(text: Any, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ── the kinds ────────────────────────────────────────────────────────────────

def grant_proposed(ctx: Context) -> List[Item]:
    from compass_core import actions as _actions
    from compass_core import grants

    out: List[Item] = []
    for row in grants.list_grants(ctx.table, ctx.pk) if ctx.table is not None else []:
        state = str(row.get("grant_status") or "")
        if state not in ("proposed", "pr_open", "revoke_pr_open"):
            continue
        grant_id = str(row.get("action_id"))
        action_type = str(row.get("action_type") or "")
        entry = _actions.describe_action(action_type)
        try:
            statements = grants.statements_for(row, ctx.config)
        except Exception:  # noqa: BLE001 - an action its captain no longer registers
            statements = []
        title_word = entry.get("title") or action_type
        if state == "proposed":
            out.append(Item(
                id=f"grant.proposed:{grant_id}", kind="grant.proposed", verb="approve", severity="normal",
                captain=str(row.get("captain") or "core"), tab="settings",
                title=f"Grant {title_word} to the {row.get('captain') or 'core'} captain",
                summary=f"{entry.get('change_type') or 'change'} · mode {row.get('mode')} · proposed by {row.get('created_by') or 'Compass'}",
                since=_ms(row.get("created_at")), subject={"grant_id": grant_id, "action_type": action_type},
                actions=[
                    Action(key="accept", label="Accept", method="POST", path=f"/api/access/grants/{grant_id}/accept", scope="admin",
                           confirm=f"Opens the access pull request that lets Compass run {action_type}."),
                    Action(key="decline", label="Decline", method="POST", path=f"/api/access/grants/{grant_id}/decline", scope="admin"),
                    Action(key="open", label="Open access", href="/workspace?tab=settings#settings-integrations"),
                ],
                detail={"statements": statements, "scope": row.get("scope") or {}, "change_type": entry.get("change_type"),
                        "rollback_supported": entry.get("rollback_supported"), "proposed_by": row.get("created_by"), "mode": row.get("mode")},
            ))
        else:
            removing = state == "revoke_pr_open"
            out.append(Item(
                id=f"grant.waiting:{grant_id}", kind="grant.waiting", verb="merge", severity="info",
                captain=str(row.get("captain") or "core"), tab="settings",
                title=f"Merge the access pull request that {'removes' if removing else 'grants'} {title_word}",
                summary=f"{row.get('grant_pr') or 'pull request pending'}",
                since=_ms(row.get("updated_at") or row.get("created_at")), subject={"grant_id": grant_id, "action_type": action_type},
                actions=[a for a in (
                    Action(key="open", label="Open pull request", href=row.get("grant_pr")) if row.get("grant_pr") else None,
                    Action(key="verify", label="Verify now", method="POST", path="/api/access/verify", scope="admin"),
                ) if a is not None],
                detail={"pull_request": row.get("grant_pr"), "statements": statements, "removing": removing},
            ))
    return out


def job_failed(ctx: Context) -> List[Item]:
    """The newest failed or dead job of each ``captain.command`` among the newest 50, unless a
    newer job of the same command completed."""
    jobs = sorted(ctx.jobs()[:JOBS_WINDOW], key=lambda j: int(j.get("updated_at") or j.get("created_at") or 0), reverse=True)
    seen: set = set()
    out: List[Item] = []
    for job in jobs:
        key = f"{job.get('captain')}.{job.get('command')}"
        status = str(job.get("status") or "").lower()
        if key in seen:
            continue
        if status == "completed":
            seen.add(key)
            continue
        if status not in _FAILED_JOB_STATUSES:
            continue
        seen.add(key)
        job_id = str(job.get("job_id"))
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        out.append(Item(
            id=f"job.failed:{job_id}", kind="job.failed", verb="fix", severity="urgent", captain=str(job.get("captain") or "core"), tab="queue",
            title=f"{key} {'died' if status == 'dead' else 'failed'}",
            summary=_short(job.get("error") or ("no heartbeat for three minutes" if status == "dead" else "see the job's events")),
            since=_ms(job.get("updated_at") or job.get("created_at")), subject={"job_id": job_id, "captain": job.get("captain"), "command": job.get("command")},
            actions=[
                Action(key="retry", label="Retry", method="POST", path=f"/api/jobs/{job_id}/restart", scope="operate"),
                Action(key="acknowledge", label="Acknowledge", method="POST", path=f"/api/inbox/job.failed:{job_id}/snooze", scope="operate", snooze=True),
                Action(key="open", label="Open job", href=f"/workspace?tab=queue&job={job_id}"),
            ],
            detail={"command": key, "error": job.get("error"), "events": list(job.get("events") or [])[-5:],
                    "requester": job.get("user_id") or payload.get("requester"), "status": status},
        ))
    return out


def access_finding(ctx: Context) -> List[Item]:
    try:
        from compass_core import access_audit
    except ImportError:
        return []
    out: List[Item] = []
    for finding in access_audit.open_findings(ctx.table, ctx.pk) if ctx.table is not None else []:
        kind = str(finding.get("kind") or "")
        fid = str(finding.get("finding_id") or f"{kind}:{finding.get('role')}")
        out.append(Item(
            id=f"access.finding:{fid}", kind="access.finding", verb="fix", severity="urgent" if kind in _URGENT_FINDING_KINDS else "normal",
            captain="core", tab="settings",
            title=f"{kind.replace('_', ' ').capitalize()} on the {finding.get('role')} role",
            summary=_short(finding.get("detail")),
            since=_ms(finding.get("first_seen")), subject={"finding_id": fid, "role": finding.get("role"), "kind": kind},
            actions=[
                Action(key="pr", label="Open the access pull request", method="POST", path="/api/access/pr", scope="admin",
                       confirm="Opens a pull request that brings the roles back to what the grants say."),
                Action(key="audit", label="Audit now", method="POST", path="/api/access/audit", scope="admin"),
                Action(key="open", label="Open access", href="/workspace?tab=settings#settings-integrations"),
            ],
            detail={k: finding.get(k) for k in ("role", "kind", "statement", "policy_arn", "principal", "detail", "first_seen", "last_seen")},
        ))
    return out


def update_available(ctx: Context) -> List[Item]:
    state = ctx.update() or {}
    if not state.get("available") or not state.get("latest"):
        return []
    latest = str(state["latest"])
    release = state.get("release") if isinstance(state.get("release"), dict) else {}
    notes = state.get("release_notes") or []
    headline = (notes[0] or {}).get("title") if notes and isinstance(notes[0], dict) else release.get("summary")
    apply_mode = str(state.get("apply") or "manual")
    primary = (Action(key="apply", label="Apply", method="POST", path="/api/updates/apply", body={"version": latest}, scope="admin",
                      confirm=f"Pulls {latest}, restarts the stack and rolls back if health fails.")
               if apply_mode == "updater" else Action(key="open", label="Open updates", href="/workspace?tab=settings#settings-updates"))
    return [Item(
        id=f"update.available:{latest}", kind="update.available", verb="later", severity="info", captain="core", tab="settings",
        title=f"Compass {latest} is out", summary=_short(headline or f"running {state.get('running')}"),
        since=_ms(release.get("date") or release.get("published_at") or state.get("checked_at")), subject={"version": latest},
        actions=[primary, Action(key="dismiss", label="Dismiss until the next release", method="POST",
                                 path=f"/api/inbox/update.available:{latest}/snooze", scope="operate", snooze=True)],
        detail={"version": latest, "running": state.get("running"), "headline": headline, "apply": apply_mode, "direct": state.get("direct"),
                "via": state.get("via"), "notes": [{"version": n.get("version"), "title": n.get("title")} for n in notes if isinstance(n, dict)][:5]},
    )]


def setup_incomplete(ctx: Context) -> List[Item]:
    """What a new install still has to connect: the read role, GitHub, the alert queue, a model."""
    config = ctx.config or {}
    aws = config.get("aws") if isinstance(config.get("aws"), dict) else {}
    roles = aws.get("roles") if isinstance(aws.get("roles"), dict) else {}
    control = aws.get("control") if isinstance(aws.get("control"), dict) else {}
    github = config.get("github") if isinstance(config.get("github"), dict) else {}
    since = _ms(config.get("created_at") or aws.get("connected_at"))
    missing: List[Tuple[str, str, str, str, str]] = []   # (slug, severity, title, summary, href)
    if not roles.get("read"):
        missing.append(("account", "urgent", "Connect the AWS account", "no read role: run `compass connect` and merge its pull request",
                        "/workspace?tab=settings#settings-integrations"))
    if not (github.get("installation_id") or str(github.get("token") or "").strip()):
        missing.append(("github", "urgent", "Connect GitHub", "no installation or token: pull requests cannot be opened",
                        "/workspace?tab=settings#settings-integrations"))
    if roles.get("read") and not any((q or {}).get("arn") for q in (control.get("alert_queues") or {}).values()):
        missing.append(("alerts", "normal", "Set up the decoy alert queue", "no alert queue: a tripped decoy would not be heard",
                        "/workspace?tab=honeypots"))
    if not ctx.model_available():
        missing.append(("model", "info", "No model is configured", "drift fixes and PR replies need ANTHROPIC_API_KEY; summaries fall back to templates",
                        "/workspace?tab=settings#settings-integrations"))
    out: List[Item] = []
    for slug, severity, title, summary, href in missing:
        actions = [Action(key="open", label="Open settings", href=href)]
        if slug == "model":
            actions.append(Action(key="acknowledge", label="Running model-free on purpose", method="POST",
                                  path="/api/inbox/setup.incomplete:model/snooze", scope="operate", snooze=True))
        out.append(Item(id=f"setup.incomplete:{slug}", kind="setup.incomplete", verb="fix", severity=severity, captain="core", tab="settings",
                        title=title, summary=summary, since=since, subject={"what": slug}, actions=actions,
                        detail={"missing": slug, "fix": summary}))
    return out


KINDS: Tuple[Tuple[str, Source], ...] = (
    ("grant", grant_proposed),
    ("job.failed", job_failed),
    ("access.finding", access_finding),
    ("update.available", update_available),
    ("setup.incomplete", setup_incomplete),
)


# ── snooze ───────────────────────────────────────────────────────────────────

def snoozes(table: Any, pk: str) -> Dict[str, Dict[str, Any]]:
    return {str(row.get("item_id")): {k: v for k, v in row.items() if k not in ("pk", "sk")} for row in _query(table, pk, inbox_snooze_prefix())}


def snooze(table: Any, pk: str, item_id: str, *, by: Optional[str] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    row = {"pk": pk, "sk": inbox_snooze_sort_key(item_id), "item_id": item_id, "kind": item_id.split(":", 1)[0],
           "snoozed_at": int(now_ms if now_ms is not None else time.time() * 1000), "snoozed_by": by or "user"}
    table.put_item(Item=row)
    invalidate()
    return {k: v for k, v in row.items() if k not in ("pk", "sk")}


def unsnooze(table: Any, pk: str, item_id: str) -> None:
    table.delete_item(Key={"pk": pk, "sk": inbox_snooze_sort_key(item_id)})
    invalidate()


def invalidate(workspace_id: Optional[str] = None) -> None:
    with _lock:
        if workspace_id is None:
            _cache.clear()
        else:
            _cache.pop(workspace_id, None)


# ── the list ─────────────────────────────────────────────────────────────────

def _captain_sources(config: Mapping[str, Any]) -> List[Tuple[str, Source]]:
    """The active captains' ``inbox`` functions, from their manifests (the decoy captain's
    parked responses, the watcher's pull requests)."""
    from compass_core import captains

    found: List[Tuple[str, Source]] = []
    for manifest in captains.manifests():
        if not manifest.inbox or not captains.is_active(manifest.name, config):
            continue
        try:
            found.append((manifest.name, captains.load_reference(manifest.inbox)))
        except Exception as exc:  # noqa: BLE001 - a captain whose module will not import has no items
            logger.warning("inbox source of %s did not load: %s", manifest.name, exc)
    return found


def _compute(ctx: Context) -> List[Item]:
    items: List[Item] = []
    with _lock:
        sources = list(KINDS) + _captain_sources(ctx.config) + list(_sources.items())
    for name, fn in sources:
        try:
            items.extend(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - one source never breaks the list
            logger.warning("inbox source %s failed: %s", name, exc)
    hidden = snoozes(ctx.table, ctx.pk) if ctx.table is not None else {}
    for item in items:
        item.snooze = hidden.get(item.id)
    items.sort(key=lambda i: (-int(i.since or 0), i.id))
    return items


def items(ctx: Context, *, tab: Optional[str] = None, kind: Optional[str] = None, verb: Optional[str] = None,
          include_snoozed: bool = False, fresh: bool = False) -> Dict[str, Any]:
    """The feed: ``{generated_at, counts, items}``. Computed at most every ``CACHE_SECONDS``
    per workspace; the filters apply to the cached list."""
    now = time.time()
    with _lock:
        cached = _cache.get(ctx.workspace_id)
    if cached and cached[0] > now and not fresh:
        _, generated_at, all_items = cached
    else:
        all_items = _compute(ctx)
        generated_at = ctx.now()
        with _lock:
            _cache[ctx.workspace_id] = (now + CACHE_SECONDS, generated_at, all_items)
    visible = [i for i in all_items if include_snoozed or not i.snooze]
    counts = {
        "total": sum(1 for i in visible if not i.snooze), "urgent": sum(1 for i in visible if i.severity == "urgent" and not i.snooze),
        "by_tab": {}, "by_verb": {v: 0 for v in VERBS},
    }
    for item in visible:
        if item.snooze:
            continue
        counts["by_tab"][item.tab] = counts["by_tab"].get(item.tab, 0) + 1
        counts["by_verb"][item.verb] = counts["by_verb"].get(item.verb, 0) + 1
    chosen = [i for i in visible if (not tab or i.tab == tab) and (not kind or i.kind == kind) and (not verb or i.verb == verb)]
    return {"generated_at": generated_at, "counts": counts, "items": [i.to_dict() for i in chosen]}


def status_line(counts: Mapping[str, Any]) -> str:
    """``Needs you: 6 (2 urgent): 2 to approve, 3 to fix, 1 later`` for ``compass status``."""
    total = int(counts.get("total") or 0)
    if not total:
        return "Needs you: nothing"
    by_verb = counts.get("by_verb") or {}
    parts = [f"{by_verb[v]} to {v}" if v != "later" else f"{by_verb[v]} later" for v in VERBS if by_verb.get(v)]
    urgent = int(counts.get("urgent") or 0)
    return f"Needs you: {total}" + (f" ({urgent} urgent)" if urgent else "") + (": " + ", ".join(parts) if parts else "")
