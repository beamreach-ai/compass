"""Commands of the core itself, the setup a new install goes through:

    compass init_config workspace=local region=us-east-1 github_repo=org/infra
    compass init_tables
    compass connect account=123456789012 region=us-east-1     # with the operator's profile, once
    compass serve

and what an install does over its life: ``compass migrate`` on a new version,
``compass update check | apply | rollback`` for the next one.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from compass_core import config as core_config
from compass_core import dispatch, sessions
from compass_core.tables import dynamo_table, tables
from compass_core.workspace import resolve

logger = logging.getLogger("compass.commands")

_PK_SK = [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]
_TYPE_INDEX = {
    "IndexName": "ResourceTypeIndex",
    "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
    "Projection": {"ProjectionType": "ALL"},
}
_TARGET_INDEX = {
    "IndexName": "TargetResourceIndex",
    "KeySchema": [{"AttributeName": "gsi2pk", "KeyType": "HASH"}, {"AttributeName": "gsi2sk", "KeyType": "RANGE"}],
    "Projection": {"ProjectionType": "ALL"},
}


def _attrs(*names: Any) -> List[Dict[str, str]]:
    """Attribute definitions; a name is a string attribute, a (name, type) pair says otherwise."""
    return [{"AttributeName": n, "AttributeType": "S"} if isinstance(n, str) else {"AttributeName": n[0], "AttributeType": n[1]} for n in names]


# ── init_config ──────────────────────────────────────────────────────────────

def _gh_token() -> Optional[str]:
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def config_template(
    *,
    workspace_id: str,
    region: str,
    github_repo: Optional[str],
    github_token: Optional[str],
    github_org: Optional[str],
    terraform_root: Optional[str],
    table_prefix: str,
    state_bucket: Optional[str] = None,
) -> Dict[str, Any]:
    github: Dict[str, Any] = {"default_branch": "main", "max_repos": 25}
    if github_token:
        github["token"] = github_token
        login = _gh_login(github_token)
        if login:
            github["login"] = login  # commits made with this token are Compass's own (captains.prs.sync)
    if github_repo:
        github["honeypot_repo"] = github_repo
        github["iac_repo"] = github_repo
        github.setdefault("org", github_repo.split("/", 1)[0])
    if github_org:
        github["org"] = github_org
    return {
        "workspace_id": workspace_id,
        "name": workspace_id,
        "target": workspace_id,
        "org_id": workspace_id,
        "aws": {"region": region, "install_id": secrets.token_hex(3), **({"state_buckets": [state_bucket]} if state_bucket else {})},
        "aws_region": region,
        "aws_regions": [region],
        "github": github,
        "honeypot": {"terraform_root": terraform_root} if terraform_root else {},
        "dynamodb_config": {
            "workspace_id": workspace_id,
            "audit": f"{table_prefix}_items_{workspace_id}",
            "items": f"{table_prefix}_items_{workspace_id}",
            "items_key": "id",
            "map": f"{table_prefix}_map_{workspace_id}",
            "aws_inventory": f"{table_prefix}_aws_inventory_{workspace_id}",
            "tf_inventory": f"{table_prefix}_tf_inventory_{workspace_id}",
            "tf_state": f"{table_prefix}_tf_state_{workspace_id}",
            "progress": f"{table_prefix}_progress_{workspace_id}",
            "jobs": f"{table_prefix}_jobs_{workspace_id}",
        },
        # the map lives in a SQLite file (infra_map.graph_store); DynamoDB keeps everything else
        "map": {"store": "sqlite"},
        "dashboard": {"username": "admin", "password": secrets.token_urlsafe(12)},
        "anthropic": {"model": "claude-opus-5-5", "effort": "medium"},
        "prs": {"watcher": {"mode": "autopilot", "risk_max": 2}},
    }


def _gh_login(token: str) -> str:
    """The GitHub login the token belongs to, or "" when GitHub cannot be reached."""
    try:
        import requests

        from compass_core.github import api_url as github_api_url

        resp = requests.get(f"{github_api_url()}/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=10)
        if resp.status_code == 200:
            return str(resp.json().get("login") or "")
    except Exception:  # noqa: BLE001 - optional nicety at install time
        pass
    return ""


def init_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write a workspace config file. Does not overwrite unless overwrite=true."""
    workspace_id = str(payload.get("workspace_id") or payload.get("workspace") or os.getenv("DEFAULT_WORKSPACE") or "local")
    out = Path(str(payload.get("out") or os.getenv("COMPASS_CONFIG") or Path("config") / f"{workspace_id}.json")).expanduser()
    if out.exists() and payload.get("overwrite") not in (True, "true", "1", 1):
        raise FileExistsError(f"{out} exists; pass overwrite=true to replace it")
    token = payload.get("github_token")
    if token in (None, "", "gh"):
        token = os.getenv("GITHUB_TOKEN") or _gh_token()
    cfg = config_template(
        workspace_id=workspace_id,
        region=str(payload.get("region") or os.getenv("AWS_REGION") or "us-east-1"),
        github_repo=payload.get("github_repo"),
        github_token=token,
        github_org=payload.get("github_org"),
        terraform_root=payload.get("terraform_root"),
        table_prefix=str(payload.get("table_prefix") or "compass"),
        state_bucket=str(payload.get("state_bucket") or "") or None,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    if os.getenv("APP_PASSWORD"):
        # .env sets the sign-in; the file does not need its own.
        cfg["dashboard"] = {"username": os.getenv("APP_USERNAME") or "admin", "password": None}
        out.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    shown = json.loads(json.dumps(cfg))
    if shown["github"].get("token"):
        shown["github"]["token"] = "<set>"
    shown["dashboard"]["password"] = "<see login>"
    source = "argument" if payload.get("github_token") else ("GITHUB_TOKEN" if os.getenv("GITHUB_TOKEN") else "gh CLI")
    return {
        "path": str(out),
        "aws_account": "not connected yet: run `compass connect account=<id> region=<region>` with your profile",
        "config": shown,
        "github_token": f"from {source}" if token else "missing",
        "login": dashboard_login(cfg),
    }


def dashboard_login(config: Dict[str, Any]) -> Dict[str, Any]:
    """Where to sign in and with what: .env (APP_USERNAME/APP_PASSWORD) wins over the config file."""
    port = os.getenv("COMPASS_PORT") or "8080"
    if os.getenv("APP_PASSWORD"):
        return {"url": f"http://localhost:{port}", "username": os.getenv("APP_USERNAME") or "admin", "password": "the APP_PASSWORD in your .env"}
    dash = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    if not dash.get("password"):
        return {"url": f"http://localhost:{port}", "warning": "no dashboard password: anyone who can reach this port can use Compass. Set APP_PASSWORD in .env."}
    return {"url": f"http://localhost:{port}", "username": dash.get("username") or "admin", "password": dash["password"]}


def login(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Show the dashboard URL and sign-in."""
    return dashboard_login(resolve(payload).config)


# ── init_tables ──────────────────────────────────────────────────────────────

def table_specs(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tables the open packages read and write, named by the workspace config.

    The map and the
    inventories carry a type index, the map also a target index and a TTL."""
    t = tables(config)
    specs: List[Dict[str, Any]] = []
    for name in {t.aws_inventory, t.tf_inventory}:
        if name:
            specs.append({"TableName": name, "KeySchema": _PK_SK, "AttributeDefinitions": _attrs("pk", "sk", "gsi1pk", "gsi1sk"), "GlobalSecondaryIndexes": [_TYPE_INDEX]})
    if t.map:
        specs.append({
            "TableName": t.map,
            "KeySchema": _PK_SK,
            "AttributeDefinitions": _attrs("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk"),
            "GlobalSecondaryIndexes": [_TYPE_INDEX, _TARGET_INDEX],
            "Ttl": "expires_at",
        })
    for name in {t.tf_state, t.items, t.audit, t.progress}:
        if name and all(s["TableName"] != name for s in specs):
            specs.append({"TableName": name, "KeySchema": _PK_SK, "AttributeDefinitions": _attrs("pk", "sk")})
    if t.jobs and all(s["TableName"] != t.jobs for s in specs):
        specs.append({
            "TableName": t.jobs,
            "KeySchema": _PK_SK,
            "AttributeDefinitions": _attrs("pk", "sk", "job_id", ("created_at", "N")),
            "GlobalSecondaryIndexes": [{
                "IndexName": "JobIdIndex",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        })
    return specs


def create_tables(config: Dict[str, Any], *, client: Any = None) -> Dict[str, List[str]]:
    """Create the missing tables; idempotent. boto3 honours AWS_ENDPOINT_URL_DYNAMODB for DynamoDB-local."""
    client = client or sessions.control().client("dynamodb")
    existing = set()
    for page in client.get_paginator("list_tables").paginate():
        existing.update(page["TableNames"])
    created, present = [], []
    for spec in table_specs(config):
        name = spec["TableName"]
        if name in existing:
            present.append(name)
            continue
        ttl = spec.pop("Ttl", None)
        client.create_table(BillingMode="PAY_PER_REQUEST", **spec)
        client.get_waiter("table_exists").wait(TableName=name)
        if ttl:
            client.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl})
        logger.info("created table %s", name)
        created.append(name)
    return {"created": created, "present": present}


def init_tables(payload: Dict[str, Any]) -> Dict[str, Any]:
    workspace = resolve(payload)
    result = create_tables(workspace.config)
    return {"workspace_id": workspace.workspace_id, **result}


def migrate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What a new version does on first start: create missing tables, run the migration steps not yet recorded."""
    from compass_core import extensions, migrations

    workspace = resolve(payload)
    root = extensions.directory()
    if root is None:
        return migrations.run(workspace.config, workspace_id=workspace.workspace_id)
    with extensions.lock(root):  # the API and the daemon start together
        return migrations.run(workspace.config, workspace_id=workspace.workspace_id)


# ── connect ──────────────────────────────────────────────────────────────────

def connect(payload: Dict[str, Any]) -> Dict[str, Any]:
    """account=<id> region=<r> [verb=template|verify|rotate]: create this installation's roles in the
    target account and its runtime identity and alert queues in the control account, verify them
    and record them. Writes to the accounts named; run it once with the operator's profile mounted."""
    from compass_core.connect import connect as _connect

    return _connect(payload)


# ── access ───────────────────────────────────────────────────────────────────

def access(payload: Dict[str, Any]) -> Dict[str, Any]:
    """list | accept id=<grant> [mode=approve|autopilot] | decline id= | remove id= | verify | audit | pr:
    the actions Compass may run in the account and their grants. accept and remove open the pull
    request that grants or revokes exactly that action (the `pr` verb is the job that opens it)."""
    from compass_core import grants as _grants
    from compass_core.keys import workspace_pk

    workspace = resolve(payload)
    config = dict(workspace.config)
    config.setdefault("workspace_id", workspace.workspace_id)
    verb = str(payload.get("verb") or "list").strip().lower()
    table = dynamo_table(tables(config).items)
    pk = workspace_pk(workspace.workspace_id)
    actor = str(payload.get("user_id") or payload.get("actor") or "") or None
    if verb == "list":
        rows = _grants.list_grants(table, pk)
        return {"workspace_id": workspace.workspace_id, "roles": config.get("aws", {}).get("roles"), "summary": _grants.summary(table, pk),
                "grants": [{k: r.get(k) for k in ("action_id", "captain", "action_type", "scope", "mode", "risk_max", "grant_status", "grant_pr", "verified_at")} for r in rows]}
    if verb == "verify":
        return {"workspace_id": workspace.workspace_id, **_grants.refresh(table, pk, config, actor=actor)}
    if verb == "pr":
        from compass_core.access_pr import open_pr

        return {"workspace_id": workspace.workspace_id, **open_pr(config, workspace_id=workspace.workspace_id, actor=actor, reason=str(payload.get("reason") or ""), table=table)}
    if verb == "audit":
        from compass_core import access_audit

        return access_audit.run(config, workspace_id=workspace.workspace_id, table=table, actor=actor)
    grant_id = str(payload.get("id") or payload.get("grant_id") or "")
    if not grant_id:
        raise ValueError(f"{verb} needs id=<grant id> (see `compass access list`)")
    try:
        if verb == "accept":
            row = _grants.accept(table, pk, grant_id, config=config, actor=actor, mode=str(payload.get("mode") or "") or None)
        elif verb == "decline":
            row = _grants.decline(table, pk, grant_id, actor=actor)
        elif verb == "remove":
            row = _grants.remove(table, pk, grant_id, config=config, actor=actor)
        else:
            raise ValueError(f"unknown verb {verb!r}: access [list | accept | decline | remove | verify | audit | pr]")
    except _grants.GrantError as exc:
        raise ValueError(str(exc)) from exc
    return {"workspace_id": workspace.workspace_id, "grant": {k: v for k, v in row.items() if k not in ("pk", "sk")}}


# ── serve ────────────────────────────────────────────────────────────────────

def serve(payload: Dict[str, Any]) -> Dict[str, Any]:
    from compass_core.serve import serve as _serve

    return _serve(payload)


def api(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Serve the open API (the dashboard's backend) until stopped: host=0.0.0.0 port=8001."""
    import uvicorn

    try:
        workspace = resolve(payload)
    except LookupError:
        workspace = None  # no workspace yet: the API serves the setup pages
    if workspace is not None:
        sessions.assert_runtime(workspace.config)  # a profile in the container is a setup mistake; say so before anything runs
    host = str(payload.get("host") or os.getenv("COMPASS_API_HOST") or "0.0.0.0")
    port = int(payload.get("port") or os.getenv("COMPASS_API_PORT") or 8001)
    uvicorn.run("compass_api.app:create_app", factory=True, host=host, port=port, log_level="info", proxy_headers=True)
    return {"stopped": True, "host": host, "port": port}


# ── captains: list, on, off, configure ──────────────────────────────────────

def _save(workspace, config: Dict[str, Any]) -> None:
    core_config.save_workspace_config(workspace.workspace_id, config)


def captains(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Every installed captain and its state for the workspace."""
    from compass_core import captain_admin
    from compass_core import captains as registry

    workspace = resolve(payload)
    return {
        "workspace_id": workspace.workspace_id,
        "core_version": registry.core_version(),
        "captains": captain_admin.summary(workspace.config),
        "incompatible": registry.incompatible(),
        "broken": registry.broken(),
    }


def _toggle(payload: Dict[str, Any], enabled: bool) -> Dict[str, Any]:
    from compass_core import captain_admin

    name = str(payload.get("captain") or payload.get("name") or "")
    if not name:
        raise ValueError("captain=<name> is required")
    workspace = resolve(payload)
    config = dict(workspace.config)
    changed = captain_admin.set_enabled(
        config, name, enabled,
        with_dependencies=payload.get("with_dependencies") in (True, "true", "1", 1),
        cascade=payload.get("cascade") in (True, "true", "1", 1),
        actor=str(payload.get("user_id") or payload.get("actor") or "") or None,
    )
    _save(workspace, config)
    return {"workspace_id": workspace.workspace_id, "changed": changed, "captain": captain_admin.state_dict(config, name)}


def enable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """captain=<name> [with_dependencies=true]: turn a captain on for the workspace."""
    return _toggle(payload, True)


def disable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """captain=<name> [cascade=true]: turn a captain off; its work is refused, nothing is deleted."""
    return _toggle(payload, False)


def configure(payload: Dict[str, Any]) -> Dict[str, Any]:
    """captain=<name> [set='{...}'] [replace=true]: show or edit the captain's config section."""
    from compass_core import captain_admin
    from compass_core import captains as registry

    name = str(payload.get("captain") or payload.get("name") or "")
    manifest = registry.manifest(name)
    if manifest is None:
        raise LookupError(f"no captain named {name!r} is installed")
    workspace = resolve(payload)
    config = dict(workspace.config)
    incoming = payload.get("set")
    if isinstance(incoming, str):
        incoming = json.loads(incoming)
    if isinstance(incoming, dict):
        captain_admin.apply_section_update(config, manifest, incoming, replace=payload.get("replace") in (True, "true", "1", 1))
        _save(workspace, config)
    return captain_admin.describe_config(config, manifest)


def catalog(payload: Dict[str, Any]) -> Dict[str, Any]:
    """[q=<words>] [refresh=true]: the captains the catalog offers and their status here."""
    from compass_core import catalog as _catalog

    config: Dict[str, Any] = {}
    try:
        config = resolve(payload).config
    except LookupError:
        pass  # no workspace yet: statuses are still right, licences read as the free plan
    return _catalog.listing(query=payload.get("q"), config=config, refresh=payload.get("refresh") in (True, "true", "1", 1))


def _catalog_entry(name: str):
    from compass_core import catalog as _catalog

    loaded = _catalog.load()
    entry = next((e for e in (loaded.entries if loaded else []) if e.name == name), None)
    if entry is None:
        raise LookupError(f"{name!r} is not in the catalog")
    return entry


def install(payload: Dict[str, Any]) -> Dict[str, Any]:
    """captain=<name>: install a catalog captain (or update it); applies at the next restart."""
    from compass_core import catalog as _catalog
    from compass_core import extensions

    name = str(payload.get("captain") or payload.get("name") or "")
    entry = _catalog_entry(name)
    if not extensions.allowed(entry.name):
        raise ValueError(f"{entry.label} is not on this install's allow-list (COMPASS_CATALOG_ALLOW)")
    status = _catalog.annotate(entry)["status"]
    if status not in ("install", "update") or not entry.pip:
        raise ValueError(f"{entry.label} is {status} here; nothing to install")
    extensions.request_install(entry.name, entry.pip, version=entry.version, by="cli")
    return {"captain": entry.name, "version": entry.version, "restart_required": True, "next": "docker compose up -d --force-recreate api compass"}


def uninstall(payload: Dict[str, Any]) -> Dict[str, Any]:
    """captain=<name>: remove a captain installed from the catalog; applies at the next restart."""
    from compass_core import extensions

    name = str(payload.get("captain") or payload.get("name") or "")
    extensions.request_uninstall(name)
    return {"captain": name, "restart_required": True, "next": "docker compose up -d --force-recreate api compass"}


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """path=<project dir>: run the checks CI runs on a captain (exit 2 when one fails)."""
    from compass_core.captain_validate import validate as _validate

    report = _validate(str(payload.get("path") or "."))
    if not report["ok"]:
        failed = [f"{r['check']}: {r['detail']}" for r in report["checks"] if not r["ok"]]
        raise ValueError("captain validation failed:\n  " + "\n  ".join(failed))
    return report


# ── update: check, apply, rollback ──────────────────────────────────────────

def update(payload: Dict[str, Any]) -> Dict[str, Any]:
    """verb=check|apply|rollback [version=…] [force=true]: is a release out; ask the updater for it; go back one."""
    from compass_core import updates

    verb = str(payload.get("verb") or "check")
    config: Dict[str, Any] = {}
    try:
        config = resolve(payload).config
    except LookupError:
        pass  # no workspace yet: the check still works, the policy reads from the environment
    force = payload.get("force") in (True, "true", "1", 1)
    try:
        if verb == "check":
            return updates.status(refresh=True, config=config)
        if verb == "apply":
            record = updates.request_apply(payload.get("version"), force=force, by="cli", config=config,
                                           wait_minutes=int(payload.get("wait_minutes") or updates.DEFAULT_WAIT_MINUTES))
            return {"requested": record, "next": "the updater applies it within a minute; `compass update check` shows the result"}
        if verb == "rollback":
            record = updates.request_rollback(by="cli")
            return {"requested": record, "next": "the updater applies it within a minute; `compass update check` shows the result"}
    except updates.UpdatesError as exc:
        lines = [str(exc)] + [f"  {c}" for c in exc.extra.get("commands") or []]
        raise ValueError("\n".join(lines)) from None
    raise ValueError("verb must be check, apply or rollback: `compass update check`")


def demo(payload: Dict[str, Any]) -> Dict[str, Any]:
    """seed [resources=10000] [seed=7]: the synthetic world on the map. Demo mode only (compass_core.demo)."""
    from compass_core import demo as _demo

    return _demo.demo(payload)


COMMANDS = {
    "init_config": init_config, "init_tables": init_tables, "serve": serve, "api": api, "login": login,
    "captains": captains, "enable": enable, "disable": disable, "configure": configure, "catalog": catalog,
    "install": install, "uninstall": uninstall, "validate": validate, "migrate": migrate, "update": update,
    "connect": connect, "access": access, "demo": demo,
}

dispatch.register("core", COMMANDS)
