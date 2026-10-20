"""The self-audit: Compass reads its own roles and flags what no manifest, grant or config
explains (docs/specs/access-model.md §9.3, D1). Every 24 hours and on demand.

1. With a read session (the core block is scoped to the two roles): both roles' trust
   policies, inline policies and attached policies.
2. The expected state: the read policy from the installed manifests, the act policy from
   the grants that should be in IAM (``pr_open`` … ``revoke_merged``: a removal is not done
   until IAM says so), the trust from the config.
3. The diff. Anything in AWS the expectation does not explain is an ``unexplained``
   finding (a statement added in the console, an attached managed policy, a second
   trusted principal); anything expected but missing is a ``missing`` finding, and an
   armed grant whose statements are gone drops to ``applied`` (``compass_core.grants``
   does that on its own re-check). A role that no connected repository holds is a
   ``not_under_code`` finding whose fix is the access pull request with its import
   blocks.
4. One row per run (``ACCESS#AUDIT#<ts>``) with the diff; a finding keeps the time it was
   first seen and closes when a run no longer sees it.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from compass_core import access_module, grants, sessions
from compass_core.aws import control_config, external_id, role_arn
from compass_core.errors import public as public_error
from compass_core.keys import workspace_pk
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("compass.access_audit")

AUDIT_PREFIX = "ACCESS#AUDIT#"
FINDING_PREFIX = "ACCESS#FINDING#"
KINDS = ("unexplained", "missing", "not_under_code", "trust")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalised(statement: Dict[str, Any]) -> str:
    s = {k: v for k, v in statement.items() if k != "Sid"}
    for key in ("Action", "Resource"):
        if key in s and isinstance(s[key], list):
            s[key] = sorted(s[key]) if len(s[key]) > 1 else s[key][0]
    return json.dumps(s, sort_keys=True)


def _role_state(iam: Any, arn: str) -> Dict[str, Any]:
    """What IAM holds for a role: trust, inline statements (by policy), attached policies."""
    name = arn.rsplit("/", 1)[-1]
    role = iam.get_role(RoleName=name)["Role"]
    trust = role.get("AssumeRolePolicyDocument") or {}
    if isinstance(trust, str):
        trust = json.loads(trust)
    inline: Dict[str, List[Dict[str, Any]]] = {}
    for policy_name in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
        document = iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]
        if isinstance(document, str):
            document = json.loads(document)
        inline[policy_name] = list(document.get("Statement", []))
    attached = [a["PolicyArn"] for a in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies", [])]
    return {"trust": trust, "inline": inline, "attached": attached}


def _principals(trust: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for statement in trust.get("Statement", []):
        principal = statement.get("Principal") or {}
        values = principal.get("AWS") if isinstance(principal, dict) else principal
        for value in (values if isinstance(values, list) else [values]):
            if value and value not in out:
                out.append(str(value))
    return out


def expected(config: Dict[str, Any], table: Any, pk: str) -> Dict[str, Any]:
    """What the roles should hold, from the manifests, the grants and the config."""
    from compass_core import captains as registry
    from compass_core import permissions

    rows = grants.list_grants(table, pk)
    read_policy = permissions.read_role_policy(registry.manifests(), config)
    act_statements: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("grant_status") or "") in grants.STILL_IN_IAM:
            act_statements.extend(grants.statements_for(row, config))
    return {
        "read": [_normalised(s) for s in read_policy["Statement"]],
        "act": [_normalised(s) for s in act_statements],
        "principal": str(control_config(config).get("runtime_arn") or ""),
        "external_id": external_id(config) or "",
        "grants": rows,
    }


def diff(config: Dict[str, Any], held: Dict[str, Dict[str, Any]], want: Dict[str, Any], *, under_code: Optional[bool]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    def finding(kind: str, role: str, detail: str, **data: Any) -> None:
        findings.append({"kind": kind, "role": role, "detail": detail, **data})

    for role in ("read", "act"):
        state = held.get(role)
        if state is None:
            continue
        expected_set = set(want[role])
        held_statements = [(name, s) for name, statements in state["inline"].items() for s in statements]
        for name, statement in held_statements:
            if _normalised(statement) not in expected_set:
                finding("unexplained", role, f"statement in inline policy {name!r} that no manifest or grant explains", statement=statement, policy=name)
        for arn in state["attached"]:
            finding("unexplained", role, f"attached managed policy {arn}", policy_arn=arn)
        held_set = {_normalised(s) for _, s in held_statements}
        for text in want[role]:
            if text not in held_set:
                finding("missing", role, "a statement the manifests or grants expect is not in the role", statement=json.loads(text))
        principals = _principals(state["trust"])
        for principal in principals:
            if principal != want["principal"]:
                finding("trust", role, f"trusted principal {principal} is not the runtime identity", principal=principal)
        if want["principal"] and want["principal"] not in principals:
            finding("trust", role, f"the runtime identity {want['principal']} is not trusted", principal=want["principal"])
        ids = {str(((s.get("Condition") or {}).get("StringEquals") or {}).get("sts:ExternalId") or "") for s in state["trust"].get("Statement", []) if s.get("Action") == "sts:AssumeRole"}
        if want["external_id"] and want["external_id"] not in ids:
            finding("trust", role, "the trust policy does not carry the external id the config holds")
        if under_code is False:
            finding("not_under_code", role, "the role is not in any connected repository: the access pull request imports it", fix="pr")
    return findings


def _finding_id(finding: Dict[str, Any]) -> str:
    import hashlib

    key = json.dumps({k: finding.get(k) for k in ("kind", "role", "statement", "policy_arn", "principal", "detail")}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def open_findings(table: Any, pk: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(FINDING_PREFIX)}
    while True:
        page = table.query(**kwargs)
        out.extend(dict(i) for i in page.get("Items", []) if not i.get("closed_at"))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(({k: v for k, v in f.items() if k not in ("pk", "sk")} for f in out), key=lambda f: (str(f.get("kind")), str(f.get("role"))))


def last_run(table: Any, pk: str) -> Optional[Dict[str, Any]]:
    page = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(AUDIT_PREFIX), ScanIndexForward=False, Limit=1)
    items = page.get("Items") or []
    return {k: v for k, v in dict(items[0]).items() if k not in ("pk", "sk")} if items else None


def under_code(config: Dict[str, Any], *, probe: Optional[Any] = None) -> Optional[bool]:
    """Whether the access module is in the connected repository's base branch: None when no
    repository is connected (nothing to be under), False when it is not there yet."""
    from compass_core.github import client as github_client
    from compass_core.github import resolve_github_token
    from compass_core.github_prs import iac_repository, iac_terraform_root

    repo_name = iac_repository(config)
    if not repo_name:
        return None
    try:
        if probe is None:
            token = resolve_github_token(config, capability="read")
            if not token:
                return None
            gh_repo = github_client(token).get_repo(repo_name)
            github_cfg = config.get("github") if isinstance(config.get("github"), dict) else {}
            base = str(github_cfg.get("default_branch") or github_cfg.get("base_branch") or "main")
            root = iac_terraform_root(config)
            path = f"{root}/{access_module.MODULE_DIR}/main.tf" if root else f"{access_module.MODULE_DIR}/main.tf"
            probe = lambda: gh_repo.get_contents(path, ref=base)  # noqa: E731
        probe()
        return True
    except Exception as exc:  # noqa: BLE001 - 404: not under code; anything else: unknown
        status = getattr(exc, "status", None)
        return False if status == 404 else None


def run(config: Dict[str, Any], *, workspace_id: str, table: Any = None, iam: Any = None, actor: Optional[str] = None,
        code_probe: Optional[Any] = None) -> Dict[str, Any]:
    """One audit: read both roles, diff against the expectation, record the run and the findings."""
    table = table or dynamo_table(tables(config).items)
    pk = workspace_pk(workspace_id)
    at = _now()
    if not role_arn(config, "read"):
        return {"at": at, "ok": False, "error": "no roles: run `compass connect`", "findings": []}
    try:
        iam = iam or sessions.target("read", captain=sessions.CORE, config=config, job_id="access-audit").client("iam")
        held = {role: _role_state(iam, arn) for role, arn in (("read", role_arn(config, "read")), ("act", role_arn(config, "act"))) if arn}
    except Exception as exc:  # noqa: BLE001
        logger.warning("access audit could not read the roles: %s", exc)
        return {"at": at, "ok": False, "error": public_error(exc, context="access audit"), "findings": []}
    want = expected(config, table, pk)
    coded = under_code(config, probe=code_probe)
    findings = diff(config, held, want, under_code=coded)
    # an armed grant whose statements are gone: the grant layer re-checks and drops it to applied
    try:
        grants.refresh(table, pk, config, iam=iam, actor="audit")
    except Exception as exc:  # noqa: BLE001
        logger.debug("grant re-check during the audit failed: %s", exc)
    # findings keep their first-seen time; ones no longer seen close
    previous = {f["finding_id"]: f for f in open_findings(table, pk)}
    current: List[Dict[str, Any]] = []
    for finding in findings:
        fid = _finding_id(finding)
        first_seen = previous.get(fid, {}).get("first_seen") or at
        row = {"pk": pk, "sk": f"{FINDING_PREFIX}{fid}", "finding_id": fid, "first_seen": first_seen, "last_seen": at, "closed_at": None, **finding}
        table.put_item(Item=row)
        current.append({k: v for k, v in row.items() if k not in ("pk", "sk")})
    for fid, old in previous.items():
        if fid not in {f["finding_id"] for f in current}:
            table.put_item(Item={"pk": pk, "sk": f"{FINDING_PREFIX}{fid}", **{k: v for k, v in old.items() if k not in ("pk", "sk")}, "closed_at": at})
    record = {"pk": pk, "sk": f"{AUDIT_PREFIX}{at}", "at": at, "by": actor or "scheduler", "findings": len(current),
              "kinds": sorted({f["kind"] for f in current}), "under_code": coded, "roles": {r: role_arn(config, r) for r in ("read", "act")},
              "diff": [{k: f.get(k) for k in ("kind", "role", "detail")} for f in current]}
    table.put_item(Item=record)
    return {"at": at, "ok": not current, "findings": current, "closed": [fid for fid in previous if fid not in {f["finding_id"] for f in current}],
            "under_code": coded, "roles": record["roles"]}


def run_service(workspace_id: str, config: Dict[str, Any], stop: threading.Event, *, interval: Optional[int] = None) -> None:
    """A `compass serve` service: the audit every 24 hours."""
    wait = float(interval or 86400)
    while True:
        try:
            if role_arn(config, "read"):
                result = run(config, workspace_id=workspace_id)
                if result.get("findings"):
                    logger.warning("access audit: %d finding(s): %s", len(result["findings"]), "; ".join(f"{f['kind']} on {f['role']}" for f in result["findings"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("access audit failed: %s", exc)
        if stop.wait(wait):
            return
