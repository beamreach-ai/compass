"""Grants: one action a captain may run in the target account, granted and revoked by a
pull request a person merges (docs/specs/access-model.md §9, D2, D3).

A grant is a row in the items table (``GRANT#<captain>#<id>``) with the action type,
its scope, the mode the gate reads (``off`` / ``approve`` / ``autopilot``), the risk
cap, and a lifecycle::

    proposed ─(accept)─▶ pr_open ─(merge)─▶ merged ─(statements in IAM)─▶ applied ─(simulate ok)─▶ verified ─▶ armed
                                                                                                          │
    revoked ◀─(gone from IAM)─ revoke_merged ◀─(merge)─ revoke_pr_open ◀───────────────── disarmed ◀──────┘

The act role's policy in the access module is the union of the grants in ``pr_open``
… ``armed``; every pull request regenerates the whole module. A merge is never taken
as an apply: the verification tick (``run_service``, every five minutes while any grant
is in flight) reads the act role back from IAM, and a grant is ``applied`` only when
its statements are there, ``verified`` when IAM's simulation allows them, ``armed``
from then on. An executor checks ``is_armed`` before the gate and before any AWS call;
a row that is not armed never reaches ``execute``, whatever its mode says. Removing an
action disarms it at once and opens the pull request that takes the permission away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from boto3.dynamodb.conditions import Key

from compass_core import actions as _actions
from compass_core import sessions
from compass_core.aws import resolve_target_account_id, resolve_target_region, role_arn
from compass_core.errors import public as public_error
from compass_core.keys import grant_prefix, grant_sort_key, workspace_pk
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("compass.grants")

MODES = ("off", "approve", "autopilot")
STATES = ("proposed", "declined", "pr_open", "merged", "applied", "verified", "armed",
          "disarmed", "revoke_pr_open", "revoke_merged", "revoked")
GRANTED = ("pr_open", "merged", "applied", "verified", "armed")          # in the access module's act policy
IN_FLIGHT = ("pr_open", "merged", "applied", "verified", "disarmed", "revoke_pr_open", "revoke_merged")
STILL_IN_IAM = GRANTED + ("disarmed", "revoke_pr_open", "revoke_merged")  # a removal is not done until IAM says so
PR_COMMAND = "access"                                                   # the core job that opens the pull request


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _epoch_ms() -> int:
    return int(time.time() * 1000)


class GrantError(ValueError):
    """A transition the lifecycle does not allow, or a grant that does not exist."""


# ── rows ──────────────────────────────────────────────────────────────────────

def list_grants(table: Any, pk: str, *, captain: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(grant_prefix(captain))}
    while True:
        page = table.query(**kwargs)
        out.extend(dict(i) for i in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(out, key=lambda r: (str(r.get("captain") or ""), str(r.get("action_type") or ""), str(r.get("created_at") or "")))


def get_grant(table: Any, pk: str, grant_id: str, *, captain: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if captain:
        found = table.get_item(Key={"pk": pk, "sk": grant_sort_key(captain, grant_id)}).get("Item")
        return dict(found) if found else None
    return next((r for r in list_grants(table, pk) if r.get("action_id") == grant_id), None)


class GrantConflict(GrantError):
    """The row changed between this writer's read and its write: another writer won."""


def _put(table: Any, row: Dict[str, Any]) -> Dict[str, Any]:
    """Write a grant only if nobody wrote it since this copy was read: every row carries a
    revision, and the write is conditional on it. The PR job, the verification tick and a
    person's click all read-modify-write the same row; a stale copy never wins."""
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError

    revision = row.get("rev")
    condition = Attr("rev").not_exists() if revision is None else Attr("rev").eq(revision)
    item = _dynamo_safe({**row, "rev": int(revision or 0) + 1})
    try:
        table.put_item(Item=item, ConditionExpression=condition)
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException":
            raise GrantConflict(f"grant {row.get('action_id')} changed while this was being written") from exc
        raise
    row["rev"] = item["rev"]
    return row


def _retrying(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """A person's action re-reads and retries when a background writer got there first."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except GrantConflict:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")

    return wrapper


def _dynamo_safe(value: Any) -> Any:
    if isinstance(value, float):
        from decimal import Decimal

        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


def _transition(row: Dict[str, Any], state: str, *, actor: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    history = list(row.get("grant_history") or [])
    history.append({"from": row.get("grant_status"), "to": state, "at": _now(), "by": actor})
    row.update(grant_status=state, grant_history=history[-20:], updated_at=_epoch_ms(), **fields)
    return row


def is_armed(row: Dict[str, Any]) -> bool:
    return str(row.get("grant_status") or "") == "armed" and bool(row.get("enabled", True))


def is_granted(row: Dict[str, Any]) -> bool:
    """Whether the access module's act policy carries this grant's statements."""
    return str(row.get("grant_status") or "") in GRANTED


# ── what a grant allows ───────────────────────────────────────────────────────

def statements_for(row: Dict[str, Any], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    action = _actions.get_action(str(row.get("action_type") or ""))
    scope = _scope_of(action, row)
    account_id = resolve_target_account_id(config) or ""
    region = str(row.get("region") or resolve_target_region(config) or "us-east-1")
    return list(action.iam_statements(scope, account_id=account_id, region=region))


def _scope_of(action: Any, row: Dict[str, Any]) -> Any:
    """The captain's scope object for the row: the decoy captain's ActionScope has from_dict;
    another captain's actions take the raw dict."""
    raw = row.get("scope") or {}
    try:
        from captains.honeypot.actions.base import ActionScope  # the first kind of action; others take the dict

        if isinstance(action, __import__("captains.honeypot.actions.base", fromlist=["ResponseAction"]).ResponseAction):
            return ActionScope.from_dict(raw)
    except Exception:  # noqa: BLE001 - the decoy captain is not installed
        pass
    return raw


def policy_hash(statements: Iterable[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(sorted(statements, key=lambda s: json.dumps(s, sort_keys=True)), sort_keys=True).encode()).hexdigest()[:16]


class Narrowing:
    """What ``sessions.target("act", action=…)`` narrows a session to: one grant's statements."""

    def __init__(self, row: Dict[str, Any], statements: Sequence[Dict[str, Any]]) -> None:
        self.grant_id = str(row.get("action_id") or "")
        self.action_type = str(row.get("action_type") or "")
        self.statements = list(statements)


def narrowing(row: Dict[str, Any], config: Dict[str, Any]) -> Narrowing:
    if not is_armed(row):
        raise GrantError(f"{row.get('action_type')} is {row.get('grant_status') or 'not granted'}, not armed: it cannot run")
    return Narrowing(row, statements_for(row, config))


# ── the lifecycle ─────────────────────────────────────────────────────────────

Enqueue = Callable[..., Any]


def _enqueue_pr(config: Dict[str, Any], workspace_id: str, *, actor: Optional[str], enqueue: Optional[Enqueue], reason: str) -> Any:
    """The pull request is a job: GitHub I/O never runs inside a request."""
    if enqueue is None:
        from compass_core.queue import job_queue

        enqueue = job_queue().enqueue
    try:
        return enqueue(captain="core", command=PR_COMMAND, payload={"verb": "pr", "workspace_id": workspace_id, "user_id": actor or "", "reason": reason})
    except Exception as exc:  # noqa: BLE001 - the row is right; the PR can be opened again from the page
        logger.warning("could not queue the access pull request: %s", exc)
        return {"error": str(exc)}


def propose(table: Any, pk: str, *, captain: str, action_type: str, scope: Dict[str, Any], mode: str = "approve",
            risk_max: int = 2, actor: Optional[str] = None, region: Optional[str] = None, proposed_by: str = "person") -> Dict[str, Any]:
    """A new grant in ``proposed``: Compass (a captain, the catalog install) or a person says
    what it would allow; nothing in AWS changes."""
    if _actions.captain_of(action_type) != captain:
        raise GrantError(f"{action_type!r} is not an action {captain} registers")
    if mode not in MODES:
        raise GrantError(f"mode {mode!r} is not one of {MODES}")
    grant_id = f"act-{uuid.uuid4().hex[:12]}"
    row = {
        "pk": pk, "sk": grant_sort_key(captain, grant_id),
        "action_id": grant_id, "captain": captain, "action_type": action_type, "scope": dict(scope or {}),
        "mode": mode, "risk_max": int(risk_max), "enabled": mode != "off",
        "grant_status": None, "grant_pr": None, "applied_at": None, "verified_at": None, "policy_hash": None,
        "created_at": _epoch_ms(), "created_by": actor, "updated_at": _epoch_ms(), "proposed_by": proposed_by,
        **({"region": region} if region else {}),
    }
    _transition(row, "proposed", actor=actor)
    return _put(table, row)


@_retrying
def accept(table: Any, pk: str, grant_id: str, *, config: Dict[str, Any], actor: Optional[str] = None, mode: Optional[str] = None,
           enqueue: Optional[Enqueue] = None) -> Dict[str, Any]:
    """A person accepts: ``pr_open``, and the pull request that grants it is queued."""
    row = get_grant(table, pk, grant_id)
    if row is None:
        raise GrantError(f"no grant {grant_id}")
    state = str(row.get("grant_status") or "")
    if state in GRANTED:
        if mode and mode != row.get("mode"):
            row.update(mode=mode, enabled=mode != "off", updated_at=_epoch_ms())
            _put(table, row)
        return row
    if state not in ("proposed", "declined", "disarmed", "revoke_pr_open", "revoke_merged", "revoked"):
        raise GrantError(f"a grant in {state} cannot be accepted")
    if mode == "off":
        raise GrantError("accepting an action means a mode of approve or autopilot")
    statements = statements_for(row, config)
    if not statements:
        raise GrantError(f"{row.get('action_type')} with this scope allows nothing: name the resources it may touch")
    row.update(mode=mode or (row.get("mode") if row.get("mode") in ("approve", "autopilot") else "approve"), enabled=True,
               policy_hash=policy_hash(statements), accepted_by=actor, accepted_at=_now())
    _transition(row, "pr_open", actor=actor)
    _put(table, row)
    row["pr_job"] = _enqueue_pr(config, _workspace_of(pk, config), actor=actor, enqueue=enqueue, reason=f"grant {row['action_type']}")
    return row


@_retrying
def decline(table: Any, pk: str, grant_id: str, *, actor: Optional[str] = None) -> Dict[str, Any]:
    row = get_grant(table, pk, grant_id)
    if row is None:
        raise GrantError(f"no grant {grant_id}")
    if str(row.get("grant_status")) != "proposed":
        raise GrantError(f"only a proposed grant can be declined (this one is {row.get('grant_status')})")
    row["enabled"] = False
    return _put(table, _transition(row, "declined", actor=actor))


@_retrying
def remove(table: Any, pk: str, grant_id: str, *, config: Dict[str, Any], actor: Optional[str] = None, enqueue: Optional[Enqueue] = None) -> Dict[str, Any]:
    """A person removes the action: disarmed at once, no AWS call; the pull request that takes
    the permission away is queued. The row stays, saying "removal waiting on <PR>", until IAM
    no longer holds the statements."""
    row = get_grant(table, pk, grant_id)
    if row is None:
        raise GrantError(f"no grant {grant_id}")
    state = str(row.get("grant_status") or "")
    row["enabled"] = False
    row["mode"] = "off"
    if state in ("proposed",):
        return _put(table, _transition(row, "declined", actor=actor))
    if state in ("declined", "revoked"):
        return _put(table, row)
    if state in ("disarmed", "revoke_pr_open", "revoke_merged"):
        return row
    _transition(row, "disarmed", actor=actor, removed_by=actor, removed_at=_now())
    _put(table, row)
    row["pr_job"] = _enqueue_pr(config, _workspace_of(pk, config), actor=actor, enqueue=enqueue, reason=f"revoke {row['action_type']}")
    return row


@_retrying
def set_mode(table: Any, pk: str, grant_id: str, *, config: Dict[str, Any], actor: Optional[str] = None,
             mode: Optional[str] = None, risk_max: Optional[int] = None, enabled: Optional[bool] = None,
             enqueue: Optional[Enqueue] = None) -> Dict[str, Any]:
    """The whitelist row's knobs: ``off`` (or enabled=false) removes, a mode of approve or
    autopilot on a removed or proposed row accepts, the risk cap changes in place."""
    row = get_grant(table, pk, grant_id)
    if row is None:
        raise GrantError(f"no grant {grant_id}")
    if risk_max is not None:
        row["risk_max"] = int(risk_max)
        row["updated_at"] = _epoch_ms()
        _put(table, row)
    wants_off = mode == "off" or enabled is False
    wants_on = (mode in ("approve", "autopilot")) or (enabled is True and mode is None and row.get("mode") != "off")
    if wants_off:
        return remove(table, pk, grant_id, config=config, actor=actor, enqueue=enqueue)
    if wants_on:
        return accept(table, pk, grant_id, config=config, actor=actor, mode=mode if mode in ("approve", "autopilot") else None, enqueue=enqueue)
    return row


def widen(table: Any, pk: str, grant_id: str, *, scope: Dict[str, Any], config: Dict[str, Any], actor: Optional[str] = None) -> Dict[str, Any]:
    """A wider scope is a new grant, proposed; the old one stays as it is until the new one is
    armed, so widening never disarms what works (the tick retires the old one then)."""
    old = get_grant(table, pk, grant_id)
    if old is None:
        raise GrantError(f"no grant {grant_id}")
    new = propose(table, pk, captain=str(old["captain"]), action_type=str(old["action_type"]), scope=scope, mode=str(old.get("mode") or "approve"),
                  risk_max=int(old.get("risk_max") or 2), actor=actor, region=old.get("region"), proposed_by="widening")
    new["supersedes"] = old["action_id"]
    _put(table, new)
    old["superseded_by"] = new["action_id"]
    _put(table, old)
    return new


def _workspace_of(pk: str, config: Dict[str, Any]) -> str:
    return str(config.get("workspace_id") or pk.split("#", 1)[-1])


# ── verification: what IAM actually holds ─────────────────────────────────────

def _iam_client(config: Dict[str, Any]) -> Any:
    session = sessions.target("read", captain=sessions.CORE, config=config, job_id="grant-verify")
    if session is None:
        raise GrantError("no read role: run `compass connect`")
    return session.client("iam")


def act_role_statements(config: Dict[str, Any], *, iam: Any = None) -> List[Dict[str, Any]]:
    """Every statement the act role holds, inline and attached, as IAM returns them."""
    arn = role_arn(config, "act")
    if not arn:
        return []
    iam = iam or _iam_client(config)
    name = arn.rsplit("/", 1)[-1]
    found: List[Dict[str, Any]] = []
    for policy_name in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
        document = iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]
        if isinstance(document, str):
            document = json.loads(document)
        found.extend(document.get("Statement", []))
    for attached in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies", []):
        version = iam.get_policy(PolicyArn=attached["PolicyArn"])["Policy"]["DefaultVersionId"]
        document = iam.get_policy_version(PolicyArn=attached["PolicyArn"], VersionId=version)["PolicyVersion"]["Document"]
        if isinstance(document, str):
            document = json.loads(document)
        found.extend(document.get("Statement", []))
    return found


def _normalised(statement: Dict[str, Any]) -> str:
    s = {k: v for k, v in statement.items() if k != "Sid"}
    for key in ("Action", "Resource"):
        if key in s and isinstance(s[key], list):
            s[key] = sorted(s[key]) if len(s[key]) > 1 else s[key][0]
    return json.dumps(s, sort_keys=True)


def _present(statements: Sequence[Dict[str, Any]], role_statements: Sequence[Dict[str, Any]]) -> bool:
    held = {_normalised(s) for s in role_statements}
    return bool(statements) and all(_normalised(s) in held for s in statements)


_CONTEXT_TYPES = (("Bool", "boolean"), ("Numeric", "numeric"), ("Date", "date"), ("IpAddress", "ip"), ("NotIpAddress", "ip"))


def context_entries(statement: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The statement's own condition as the simulation's context: a request that meets it.
    Without it IAM answers ``implicitDeny`` for every conditioned statement (``ecs:cluster``
    on a task stop) and the grant never arms."""
    entries: List[Dict[str, Any]] = []
    for operator, keys in (statement.get("Condition") or {}).items():
        if not isinstance(keys, dict):
            continue
        kind = next((t for prefix, t in _CONTEXT_TYPES if str(operator).startswith(prefix)), "string")
        for key, values in keys.items():
            values = values if isinstance(values, list) else [values]
            entries.append({"ContextKeyName": key, "ContextKeyValues": [str(v) for v in values], "ContextKeyType": kind})
    return entries


def simulate(iam: Any, role_arn_value: str, statements: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """IAM's own answer for each statement; ``simulated: False`` when the API is not there (moto)."""
    denied: List[Dict[str, Any]] = []
    try:
        for statement in statements:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            kwargs: Dict[str, Any] = {"PolicySourceArn": role_arn_value, "ActionNames": actions}
            if resources != ["*"]:
                kwargs["ResourceArns"] = resources
            context = context_entries(statement)
            if context:
                kwargs["ContextEntries"] = context
            for evaluation in iam.simulate_principal_policy(**kwargs).get("EvaluationResults", []):
                if evaluation.get("EvalDecision") != "allowed":
                    denied.append({"action": evaluation.get("EvalActionName"), "resource": evaluation.get("EvalResourceName"), "decision": evaluation.get("EvalDecision")})
    except Exception as exc:  # noqa: BLE001
        return {"simulated": False, "denied": [], "error": public_error(exc, context="IAM simulation")}
    return {"simulated": True, "denied": denied}


def _pr_state(config: Dict[str, Any], workspace_id: str, pr_url: Optional[str], registry: Any = None) -> Optional[str]:
    if not pr_url:
        return None
    try:
        if registry is None:
            from compass_core.registry import PRRegistry

            registry = PRRegistry.from_config(config, workspace_id=workspace_id)
        record = registry.get_by_url(pr_url)
        return str(record.get("state")) if record else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("pull request state unknown for %s: %s", pr_url, exc)
        return None


def refresh(table: Any, pk: str, config: Dict[str, Any], *, iam: Any = None, registry: Any = None, captain: Optional[str] = None,
            actor: Optional[str] = None) -> Dict[str, Any]:
    """Move every in-flight grant as far as the pull request and IAM allow. Reads only."""
    rows = [r for r in list_grants(table, pk, captain=captain) if str(r.get("grant_status") or "") in IN_FLIGHT + ("armed",)]
    result: Dict[str, Any] = {"checked": len(rows), "changed": [], "simulated": None, "errors": []}
    if not rows:
        return result
    workspace_id = _workspace_of(pk, config)
    try:
        iam = iam or _iam_client(config)
        held = act_role_statements(config, iam=iam)
    except Exception as exc:  # noqa: BLE001 - no role yet, or no network
        result["errors"].append(public_error(exc, context="reading the act role"))
        return result
    arn = role_arn(config, "act") or ""
    for row in rows:
        before = str(row.get("grant_status"))
        state = before
        statements = statements_for(row, config)
        present = _present(statements, held)
        if state == "pr_open" and _pr_state(config, workspace_id, row.get("grant_pr"), registry) == "merged":
            state = "merged"
        if state in ("pr_open", "merged") and present:
            state = "applied"
            row["applied_at"] = _now()
        if state == "applied":
            verdict = simulate(iam, arn, statements)
            result["simulated"] = verdict["simulated"]
            row["verification"] = verdict
            if verdict["simulated"] and not verdict["denied"]:
                state = "verified"
            elif not verdict["simulated"]:
                state = "verified"   # the statements are in the role; IAM's simulation is not available here (moto), and says so
            if state == "verified":
                row["verified_at"] = _now()
        if state == "verified":
            state = "armed"
            row["armed_at"] = _now()
        if state == "disarmed" and _pr_state(config, workspace_id, row.get("grant_pr"), registry) == "merged":
            state = "revoke_merged"
        if state in ("disarmed", "revoke_pr_open", "revoke_merged") and not present:
            state = "revoked"
            row["revoked_at"] = _now()
        if state == "armed" and not present and before == "armed":
            state = "applied"   # the self-audit's case: a grant that says armed but is not in the role
        if state != before:
            _transition(row, state, actor=actor or "tick")
            try:
                _put(table, row)
            except GrantConflict:
                result.setdefault("conflicts", []).append(row["action_id"])   # someone acted meanwhile; the next pass re-reads
                continue
            result["changed"].append({"grant_id": row["action_id"], "action_type": row["action_type"], "from": before, "to": state})
            if state == "armed" and row.get("supersedes"):
                old = get_grant(table, pk, str(row["supersedes"]))
                if old is not None and str(old.get("grant_status")) in GRANTED:
                    remove(table, pk, str(old["action_id"]), config=config, actor="widening")
    return result


def verify_now(config: Dict[str, Any], *, workspace_id: str, captain: Optional[str] = None, actor: Optional[str] = None, table: Any = None) -> Dict[str, Any]:
    table = table or dynamo_table(tables(config).items)
    return refresh(table, workspace_pk(workspace_id), config, captain=captain, actor=actor)


def summary(table: Any, pk: str) -> Dict[str, Any]:
    rows = list_grants(table, pk)
    return {
        "total": len(rows),
        "armed": sum(1 for r in rows if is_armed(r)),
        "in_flight": sum(1 for r in rows if str(r.get("grant_status")) in IN_FLIGHT),
        "waiting_on": sorted({str(r.get("grant_pr")) for r in rows if str(r.get("grant_status")) in ("pr_open", "revoke_pr_open") and r.get("grant_pr")}),
    }


# ── the service: every five minutes while a grant is in flight ───────────────

def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """A `compass serve` service (compass_core.captains.ServiceSpec): a merged pull request arms
    an action without anyone remembering to click."""
    wait = float(interval or 300)
    while True:
        try:
            if role_arn(config, "act"):
                result = verify_now(config, workspace_id=workspace_id)
                if result["changed"]:
                    logger.info("grants: %s", ", ".join(f"{c['action_type']} {c['from']}→{c['to']}" for c in result["changed"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("grant verification failed: %s", exc)
        if stop.wait(wait):
            return
