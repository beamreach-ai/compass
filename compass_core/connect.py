"""``compass connect``: the operator's profile, used once, creates what Compass needs in
the target account and nothing else (docs/specs/access-model.md §4, §6, §11, D1, D7).

    docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<profile> \\
        compass connect account=123456789012 region=us-east-1

What it leaves behind, idempotently (a second run reports and changes nothing):

- in the **target** account, two roles for this installation, ``svc-inventory-reader-<install>``
  with the generated read policy (``compass_core.permissions.read_role_policy``) and
  ``svc-ops-responder-<install>`` with no policy yet (the grant flow fills it), both
  trusting the base identity with an external id and ``sts:SetSourceIdentity``;
- in the **control** account (the profile's, or ``control_account=``), one alert queue
  per region with SQS-managed encryption and a policy that lets the target account's
  own roles deliver, and, when the machine has no AWS role of its own, the runtime
  user ``svc-compass-runtime-<install>`` whose only rights are AssumeRole on the two
  roles and the queues. Its key goes into ``config/local.json``.

Then it assumes both roles as the base identity, asks IAM to simulate the read
policy, and writes the ``aws`` block of the config. ``connect template`` prints the
same as CloudFormation and Terraform for an account the operator cannot reach from
here; ``connect verify`` runs the check alone; ``connect rotate`` replaces the
runtime user's key.

Every call here writes to an AWS account; it is the one command that does.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from compass_core import config as core_config
from compass_core import grants, permissions, sessions
from compass_core.aws import _aws_config, alert_queues, control_config, resolve_target_regions
from compass_core.errors import public as public_error
from compass_core.workspace import resolve

logger = logging.getLogger("compass.connect")

ROLE_PATH = "/service/"
READ_ROLE = "svc-inventory-reader"
ACT_ROLE = "svc-ops-responder"
RUNTIME_USER = "svc-compass-runtime"
QUEUE_PREFIX = "compass-decoy-alerts"
READ_POLICY_NAME = "inventory-read"
RUNTIME_POLICY_NAME = "runtime"
TAG = "managed-by"
ASSUME_RETRY_SECONDS = 30


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _flag(payload: Dict[str, Any], key: str) -> bool:
    return payload.get(key) in (True, "true", "1", 1)


def _install_id(aws_cfg: Dict[str, Any]) -> str:
    value = str(aws_cfg.get("install_id") or "").strip()
    if not value:
        value = secrets.token_hex(3)
        aws_cfg["install_id"] = value
    return value


def _external_id(aws_cfg: Dict[str, Any]) -> str:
    for candidate in (aws_cfg.get("external_id"), aws_cfg.get("generated_external_id"), (aws_cfg.get("assume_role") or {}).get("external_id")):
        if isinstance(candidate, str) and candidate.strip():
            aws_cfg["external_id"] = candidate.strip()
            return candidate.strip()
    aws_cfg["external_id"] = f"beamreach-compass-{secrets.token_urlsafe(18)}"
    return aws_cfg["external_id"]


# ── the documents ─────────────────────────────────────────────────────────────

def trust_policy(base_identity_arn: str, external_id: str) -> Dict[str, Any]:
    """The base identity may assume the role with the external id, and may set a
    SourceIdentity on the way in (the condition key does not apply to that action)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Assume", "Effect": "Allow", "Principal": {"AWS": base_identity_arn}, "Action": "sts:AssumeRole",
             "Condition": {"StringEquals": {"sts:ExternalId": external_id}}},
            {"Sid": "SourceIdentity", "Effect": "Allow", "Principal": {"AWS": base_identity_arn}, "Action": "sts:SetSourceIdentity"},
        ],
    }


def runtime_policy(role_arns: List[str], queue_arns: List[str]) -> Dict[str, Any]:
    """Everything the runtime user may do: assume the two roles, read the alert queues, ask who it is."""
    statements: List[Dict[str, Any]] = [
        {"Sid": "AssumeCompassRoles", "Effect": "Allow", "Action": "sts:AssumeRole", "Resource": sorted(role_arns)},
        {"Sid": "WhoAmI", "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
    ]
    if queue_arns:
        statements.append({"Sid": "AlertQueues", "Effect": "Allow",
                           "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
                           "Resource": sorted(queue_arns)})
    return {"Version": "2012-10-17", "Statement": statements}


def queue_policy(queue_arn: str, *, target_account: str, base_identity_arn: str) -> Dict[str, Any]:
    """Who may use the alert queue: the target account's own roles (its IAM decides which:
    the decoy modules' alert_delivery roles and the listener's task role), its EventBridge
    rules, and the base identity, which reads."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "TargetAccountDelivery", "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{target_account}:root"},
             "Action": "sqs:SendMessage", "Resource": queue_arn},
            {"Sid": "DecoyEventBridgeRules", "Effect": "Allow", "Principal": {"Service": "events.amazonaws.com"},
             "Action": "sqs:SendMessage", "Resource": queue_arn, "Condition": {"StringEquals": {"aws:SourceAccount": target_account}}},
            {"Sid": "BaseIdentityReads", "Effect": "Allow", "Principal": {"AWS": base_identity_arn},
             "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": queue_arn},
        ],
    }


# ── the machine's own identity (D7) ───────────────────────────────────────────

def detect_machine_role(timeout: float = 1.0) -> Optional[str]:
    """The role this machine runs with, if any: the ECS task role from the container
    credentials endpoint, else the EC2 instance role from IMDSv2. None off AWS. An
    instance whose metadata hop limit hides it from containers reads as None too."""
    import os
    import urllib.request

    relative = os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    full = os.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    url = full or (f"http://169.254.170.2{relative}" if relative else None)
    try:
        if url:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - the ECS endpoint
                return str(json.load(response).get("RoleArn") or "") or None
        token_request = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                               headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(token_request, timeout=timeout) as response:  # noqa: S310 - IMDSv2
            token = response.read().decode()
        info_request = urllib.request.Request("http://169.254.169.254/latest/meta-data/iam/info", headers={"X-aws-ec2-metadata-token": token})
        with urllib.request.urlopen(info_request, timeout=timeout) as response:  # noqa: S310
            profile_arn = str(json.load(response).get("InstanceProfileArn") or "")
        if not profile_arn:
            return None
        # the instance profile's role has the profile's name in every stack this project ships; the caller resolves it with IAM
        return profile_arn.replace(":instance-profile/", ":role/")
    except Exception:  # noqa: BLE001 - not on AWS, or the endpoint is out of reach
        return None


# ── the command ───────────────────────────────────────────────────────────────

def connect(payload: Dict[str, Any]) -> Dict[str, Any]:
    """account=<id> region=<r> [regions=…] [control_account=<id>] [base_identity=<arn>|none] [read_role=…] [act_role=…] [runtime_user=…]:
    create this installation's roles in the target account, the alert queues and the runtime
    identity in the control account, verify them, and record them. Writes to the accounts
    named; run it with the operator's profile mounted, once. Verbs: template | verify | rotate."""
    verb = str(payload.get("verb") or "run").strip().lower()
    if verb == "template":
        return template(payload)
    if verb == "verify":
        return verify(payload)
    if verb == "rotate":
        return rotate(payload)
    if verb != "run":
        raise ValueError(f"unknown verb {verb!r}: connect [template | verify | rotate]")
    return Connector(payload).run()


class Connector:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.workspace = resolve(payload)
        self.config: Dict[str, Any] = dict(self.workspace.config)
        self.aws_cfg: Dict[str, Any] = dict(_aws_config(self.config))
        self.config["aws"] = self.aws_cfg
        self.steps: List[Dict[str, Any]] = []
        self.profile = sessions.profile(region=str(payload.get("region") or self.aws_cfg.get("region") or "us-east-1"))

    def step(self, name: str, detail: str, **data: Any) -> None:
        self.steps.append({"step": name, "detail": detail, **data})
        logger.info("connect: %s: %s", name, detail)

    # 1. identify
    def identify(self) -> Dict[str, str]:
        identity = self.profile.client("sts").get_caller_identity()
        arn = str(identity["Arn"])
        runtime_arn = str(control_config(self.config).get("runtime_arn") or "")
        if runtime_arn and sessions._same_identity(arn, runtime_arn):
            raise RuntimeError(f"these are the runtime identity's credentials ({arn}); connect runs with the operator's profile, not with them")
        self.step("identify", f"running as {arn}", arn=arn, account=str(identity["Account"]))
        return {"arn": arn, "account": str(identity["Account"])}

    def run(self) -> Dict[str, Any]:
        who = self.identify()
        target_account = str(self.payload.get("account") or self.aws_cfg.get("account_id") or who["account"]).strip()
        region = str(self.payload.get("region") or self.aws_cfg.get("region") or "us-east-1").strip()
        regions = [str(r) for r in (self.payload.get("regions") or [])] if isinstance(self.payload.get("regions"), list) else \
            [r for r in str(self.payload.get("regions") or "").split(",") if r] or resolve_target_regions({"aws": {**self.aws_cfg, "region": region}})
        if region not in regions:
            regions.insert(0, region)
        control_account = str(self.payload.get("control_account") or who["account"]).strip()
        install_id = _install_id(self.aws_cfg)
        external_id = _external_id(self.aws_cfg)
        prefix = str(self.aws_cfg.get("session_prefix") or sessions.DEFAULT_PREFIX)
        read_role = str(self.payload.get("read_role") or self.aws_cfg.get("read_role_name") or f"{READ_ROLE}-{install_id}")
        act_role = str(self.payload.get("act_role") or self.aws_cfg.get("act_role_name") or f"{ACT_ROLE}-{install_id}")
        runtime_user = str(self.payload.get("runtime_user") or f"{RUNTIME_USER}-{install_id}")
        roles = dict(self.aws_cfg.get("roles") or {})
        read_arn = f"arn:aws:iam::{target_account}:role{ROLE_PATH}{read_role}"
        act_arn = roles.get("act") or f"arn:aws:iam::{target_account}:role{ROLE_PATH}{act_role}"
        self.aws_cfg.update({"provider": "aws", "account_id": target_account, "region": region, "regions": regions,
                             "external_id": external_id, "session_prefix": prefix, "read_role_name": read_role, "act_role_name": act_role})
        if "state_buckets" not in self.aws_cfg and self.payload.get("state_bucket"):
            self.aws_cfg["state_buckets"] = [str(self.payload["state_bucket"])]

        # 2. the control side
        before = alert_queues(self.config)
        control = dict(control_config(self.config))
        control["account_id"] = control_account
        base_arn, source = self.base_identity(control, control_account, runtime_user)
        queues = self.alert_queues(regions, control_account=control_account, target_account=target_account, base_identity_arn=base_arn)
        control["alert_queues"] = queues
        if source == "access_key":
            self.runtime_user(control, runtime_user, role_arns=[read_arn, act_arn], queue_arns=[q["arn"] for q in queues.values()])
        self.aws_cfg["control"] = control

        # 3. the target side
        self.aws_cfg["roles"] = {"read": read_arn, "act": act_arn}
        self.roles(read_role=read_role, act_role=act_role, act_arn=act_arn, base_identity_arn=base_arn, external_id=external_id, prefix=prefix)
        self.aws_cfg["source_identity"] = True
        self.aws_cfg.setdefault("connected_at", _now())

        # 4. verify
        verification = self.verify_roles(source)
        self.aws_cfg["verified_at"] = _now() if verification.get("ok") else None
        self.aws_cfg["verification"] = verification

        # decoys report to aws.control.alert_queues from here on; ones planted before this point keep the old queue
        old = sorted({q["arn"] for q in before.values()} - {q["arn"] for q in queues.values()})
        if old:
            self.step("alerts", f"decoys planted before this point still report to {', '.join(old)} until `honeypot replant-alerts` runs", old_queue_arns=old)
        for stale in ("auth_mode", "assume_role", "assume_role_read", "assume_role_write", "assume_role_respond", "credentials",
                      "credentials_read", "credentials_write", "credentials_respond", "generated_external_id"):
            self.aws_cfg.pop(stale, None)

        # 5. hand over
        core_config.save_workspace_config(self.workspace.workspace_id, self.config)
        sessions.clear_cache()
        next_steps = ["docker compose up -d", "the profile is no longer needed: remove AWS_PROFILE from .env"]
        if not verification.get("ok"):
            next_steps.insert(0, "fix what the verification table shows, then `compass connect verify`")
        self.step("done", "config written", path=str(core_config.config_path(self.workspace.workspace_id) or ""))
        shown = json.loads(json.dumps(self.aws_cfg))
        if (shown.get("control") or {}).get("credentials"):
            shown["control"]["credentials"] = {"access_key_id": shown["control"]["credentials"].get("access_key_id"), "secret_access_key": "<set>"}
        return {"workspace_id": self.workspace.workspace_id, "steps": self.steps, "aws": shown, "verification": verification, "next": next_steps}

    # ── the control side ──────────────────────────────────────────────────────

    def base_identity(self, control: Dict[str, Any], control_account: str, runtime_user: str) -> Tuple[str, str]:
        """(ARN, credential_source): `base_identity=` wins, else the machine's own role, else
        the runtime user this run creates."""
        wanted = str(self.payload.get("base_identity") or "").strip()
        if wanted and wanted.lower() != "none":
            control.update({"credential_source": "ambient", "runtime_arn": wanted})
            control.pop("credentials", None)
            self.step("base identity", f"as given: {wanted}", arn=wanted, source="ambient")
            return wanted, "ambient"
        detected = None if wanted.lower() == "none" else detect_machine_role()
        if detected:
            control.update({"credential_source": "ambient", "runtime_arn": detected})
            control.pop("credentials", None)
            self.step("base identity", f"this machine's own role: {detected}; no runtime user is created", arn=detected, source="ambient")
            return detected, "ambient"
        arn = f"arn:aws:iam::{control_account}:user{ROLE_PATH}{runtime_user}"
        control.update({"credential_source": "access_key", "runtime_arn": arn})
        self.step("base identity", f"no AWS role on this machine: the runtime user {arn}", arn=arn, source="access_key")
        return arn, "access_key"

    def alert_queues(self, regions: List[str], *, control_account: str, target_account: str, base_identity_arn: str) -> Dict[str, Dict[str, str]]:
        queues: Dict[str, Dict[str, str]] = {}
        name = f"{QUEUE_PREFIX}-{self.workspace.workspace_id}"
        for region in regions:
            sqs = self.profile.client("sqs", region_name=region)
            url = sqs.create_queue(QueueName=name, Attributes={"MessageRetentionPeriod": "345600", "SqsManagedSseEnabled": "true"})["QueueUrl"]
            arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            if arn.split(":")[4] != control_account:
                raise RuntimeError(f"the profile created the queue in account {arn.split(':')[4]}, not the control account {control_account}: "
                                   "run connect with a profile for the control account")
            sqs.set_queue_attributes(QueueUrl=url, Attributes={
                "Policy": json.dumps(queue_policy(arn, target_account=target_account, base_identity_arn=base_identity_arn)),
                "SqsManagedSseEnabled": "true",
            })
            queues[region] = {"arn": arn, "url": url}
            self.step("alert queue", f"{arn} ({region}), SSE-SQS, the target account's roles may send", region=region, arn=arn)
        return queues

    def runtime_user(self, control: Dict[str, Any], user_name: str, *, role_arns: List[str], queue_arns: List[str]) -> None:
        iam = self.profile.client("iam")
        try:
            iam.create_user(UserName=user_name, Path=ROLE_PATH, Tags=[{"Key": TAG, "Value": self.aws_cfg.get("session_prefix") or sessions.DEFAULT_PREFIX}])
            created = True
        except iam.exceptions.EntityAlreadyExistsException:
            created = False
        iam.put_user_policy(UserName=user_name, PolicyName=RUNTIME_POLICY_NAME, PolicyDocument=json.dumps(runtime_policy(role_arns, queue_arns)))
        existing = control.get("credentials") if isinstance(control.get("credentials"), dict) else {}
        known = {k["AccessKeyId"] for k in iam.list_access_keys(UserName=user_name)["AccessKeyMetadata"]}
        if existing.get("access_key_id") in known and existing.get("secret_access_key"):
            self.step("runtime user", f"{user_name}: present, its key in the config", created=False, rotated=False)
            return
        key = iam.create_access_key(UserName=user_name)["AccessKey"]
        control["credentials"] = {"access_key_id": key["AccessKeyId"], "secret_access_key": key["SecretAccessKey"], "created_at": _now()}
        self.step("runtime user", f"{user_name}: {'created' if created else 'present'}, a new access key in the config", created=created, rotated=True)

    # ── the target side ───────────────────────────────────────────────────────

    def roles(self, *, read_role: str, act_role: str, act_arn: str, base_identity_arn: str, external_id: str, prefix: str) -> None:
        iam = self.profile.client("iam")
        trust = json.dumps(trust_policy(base_identity_arn, external_id))
        tags = [{"Key": TAG, "Value": prefix}]
        for role_name, kind in ((read_role, "read"), (act_role, "act")):
            if kind == "act" and act_arn.rsplit("/", 1)[-1] != act_role:
                self.step("act role", f"keeping the existing responder role {act_arn}", arn=act_arn)
                continue
            try:
                iam.create_role(RoleName=role_name, Path=ROLE_PATH, AssumeRolePolicyDocument=trust, Tags=tags,
                                Description=f"Compass {kind} role for installation {self.aws_cfg.get('install_id')}")
                created = True
            except iam.exceptions.EntityAlreadyExistsException:
                iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=trust)
                created = False
            if kind == "read":
                from compass_core import captains

                policy = permissions.read_role_policy(captains.manifests(), self.config)
                iam.put_role_policy(RoleName=role_name, PolicyName=READ_POLICY_NAME, PolicyDocument=json.dumps(policy))
                self.step("read role", f"{role_name}: {'created' if created else 'present'}; policy from {len(policy['Statement'])} statements",
                          created=created, statements=len(policy["Statement"]))
            else:
                self.step("act role", f"{role_name}: {'created' if created else 'present'}, no permissions until a grant is merged", created=created)

    # ── verify ────────────────────────────────────────────────────────────────

    def verify_roles(self, source: str) -> Dict[str, Any]:
        return verify_with(self.config, self.profile if source != "access_key" else None, steps=self.steps)


def _wait_assume(config: Dict[str, Any], capability: str, *, steps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Assume a role as the base identity, waiting for IAM to notice a user or a trust
    policy created seconds ago."""
    deadline = time.time() + ASSUME_RETRY_SECONDS
    last: Exception = RuntimeError("not attempted")
    while time.time() < deadline:
        try:
            sessions.clear_cache()
            session = sessions.target(capability, captain=sessions.CORE, config=config, job_id="connect")
            if session is None:
                return {"ok": False, "detail": f"no {capability} role in the config"}
            arn = session.client("sts").get_caller_identity()["Arn"]
            return {"ok": True, "arn": arn}
        except Exception as exc:  # noqa: BLE001 - AccessDenied until IAM has propagated
            last = exc
            time.sleep(2)
    return {"ok": False, "detail": public_error(last, context=f"assuming the {capability} role")}


def verify_with(config: Dict[str, Any], profile: Any, *, steps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Assume both roles as the base identity (when it is the runtime user; a machine role
    cannot be assumed from here, its first start checks) and ask IAM to simulate the read
    policy. `simulated: False` says the simulation was not available (moto), not that it failed."""
    from compass_core import captains

    sessions.clear_cache()
    result: Dict[str, Any] = {"ok": True, "assumed": {}, "simulated": None, "denied": []}
    if profile is None:
        for capability in ("read", "act"):
            outcome = _wait_assume(config, capability)
            result["assumed"][capability] = outcome
            result["ok"] = result["ok"] and outcome["ok"]
            if steps is not None:
                steps.append({"step": f"assume {capability}", "detail": outcome.get("arn") or outcome.get("detail"), **outcome})
    else:
        result["assumed"] = {"skipped": "the base identity is this machine's role; `serve` checks it at start"}
    policy = permissions.read_role_policy(captains.manifests(), config)
    read_arn = (_aws_config(config).get("roles") or {}).get("read")
    try:
        iam = (profile or sessions.target("read", captain=sessions.CORE, config=config, job_id="connect")).client("iam")
        denied: List[Dict[str, Any]] = []
        for statement in policy["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            kwargs: Dict[str, Any] = {"PolicySourceArn": read_arn, "ActionNames": actions}
            if resources != ["*"]:
                kwargs["ResourceArns"] = resources
            context = grants.context_entries(statement)
            if context:
                kwargs["ContextEntries"] = context  # a conditioned statement is implicitDeny without its context
            for evaluation in iam.simulate_principal_policy(**kwargs).get("EvaluationResults", []):
                if evaluation.get("EvalDecision") != "allowed":
                    denied.append({"action": evaluation.get("EvalActionName"), "resource": evaluation.get("EvalResourceName"), "decision": evaluation.get("EvalDecision")})
        result["simulated"], result["denied"] = True, denied
        result["ok"] = result["ok"] and not denied
    except Exception as exc:  # noqa: BLE001 - moto has no SimulatePrincipalPolicy; a real denial is in `denied`
        result["simulated"], result["simulation_error"] = False, public_error(exc, context="simulating the read policy")
    if steps is not None:
        steps.append({"step": "simulate", "detail": "read policy simulated" if result["simulated"] else "simulation not available here", **{k: result[k] for k in ("simulated", "denied")}})
    return result


def verify(payload: Dict[str, Any]) -> Dict[str, Any]:
    """connect verify: assume the roles as the base identity and simulate the read policy, nothing else."""
    workspace = resolve(payload)
    config = dict(workspace.config)
    if not (_aws_config(config).get("roles") or {}).get("read"):
        raise RuntimeError("no roles in the config: run `compass connect` first")
    source = control_config(config).get("credential_source", "access_key")
    verification = verify_with(config, sessions.profile() if source == "ambient" and payload.get("with_profile") else None)
    aws_cfg = dict(_aws_config(config))
    aws_cfg["verified_at"] = _now() if verification.get("ok") else None
    aws_cfg["verification"] = verification
    config["aws"] = aws_cfg
    core_config.save_workspace_config(workspace.workspace_id, config)
    return {"workspace_id": workspace.workspace_id, "verification": verification, "roles": aws_cfg.get("roles")}


def rotate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """connect rotate: a new access key for the runtime user, the old one deleted. Run with the operator's profile."""
    workspace = resolve(payload)
    config = dict(workspace.config)
    control = dict(control_config(config))
    if control.get("credential_source", "access_key") != "access_key" or not control.get("runtime_arn"):
        raise RuntimeError("the base identity is not a runtime user; nothing to rotate")
    user_name = str(control["runtime_arn"]).rsplit("/", 1)[-1]
    iam = sessions.profile().client("iam")
    old = (control.get("credentials") or {}).get("access_key_id")
    key = iam.create_access_key(UserName=user_name)["AccessKey"]
    control["credentials"] = {"access_key_id": key["AccessKeyId"], "secret_access_key": key["SecretAccessKey"], "created_at": _now()}
    if old:
        try:
            iam.delete_access_key(UserName=user_name, AccessKeyId=old)
        except Exception as exc:  # noqa: BLE001 - already gone
            logger.warning("old key %s not deleted: %s", old, exc)
    aws_cfg = dict(_aws_config(config))
    aws_cfg["control"] = control
    config["aws"] = aws_cfg
    core_config.save_workspace_config(workspace.workspace_id, config)
    sessions.clear_cache()
    return {"workspace_id": workspace.workspace_id, "runtime_arn": control["runtime_arn"], "access_key_id": key["AccessKeyId"], "replaced": old,
            "next": ["docker compose restart compass api"]}


# ── no profile at hand ────────────────────────────────────────────────────────

def template(payload: Dict[str, Any]) -> Dict[str, Any]:
    """connect template account=<id> region=<r> base_identity=<arn>: the two roles as a
    CloudFormation template and a Terraform module, for an account the operator cannot
    reach from here. The queue and the runtime user are the control account's: run
    `connect` there, or pass the base identity it created."""
    import yaml

    workspace = resolve(payload)
    config = dict(workspace.config)
    aws_cfg = dict(_aws_config(config))
    config["aws"] = aws_cfg
    account = str(payload.get("account") or aws_cfg.get("account_id") or "").strip()
    if not account:
        raise ValueError("account=<target account id> is required")
    base = str(payload.get("base_identity") or control_config(config).get("runtime_arn") or "").strip()
    if not base:
        raise ValueError("base_identity=<arn> is required: the identity Compass runs with (the runtime user `connect` created, or the machine's role)")
    install_id = _install_id(aws_cfg)
    external = _external_id(aws_cfg)
    read_role = str(payload.get("read_role") or f"{READ_ROLE}-{install_id}")
    act_role = str(payload.get("act_role") or f"{ACT_ROLE}-{install_id}")
    aws_cfg.update({"account_id": account, "region": str(payload.get("region") or aws_cfg.get("region") or "us-east-1"),
                    "roles": {"read": f"arn:aws:iam::{account}:role{ROLE_PATH}{read_role}", "act": f"arn:aws:iam::{account}:role{ROLE_PATH}{act_role}"},
                    "read_role_name": read_role, "act_role_name": act_role, "source_identity": True})
    from compass_core import captains

    read_policy = permissions.read_role_policy(captains.manifests(), config)
    trust = trust_policy(base, external)
    prefix = aws_cfg.get("session_prefix") or sessions.DEFAULT_PREFIX
    cloudformation = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"Compass installation {install_id}: the read role and the act role",
        "Resources": {
            "ReadRole": {"Type": "AWS::IAM::Role", "Properties": {
                "RoleName": read_role, "Path": ROLE_PATH, "AssumeRolePolicyDocument": trust,
                "Policies": [{"PolicyName": READ_POLICY_NAME, "PolicyDocument": read_policy}],
                "Tags": [{"Key": TAG, "Value": prefix}]}},
            "ActRole": {"Type": "AWS::IAM::Role", "Properties": {
                "RoleName": act_role, "Path": ROLE_PATH, "AssumeRolePolicyDocument": trust,
                "Tags": [{"Key": TAG, "Value": prefix}]}},
        },
        "Outputs": {"ReadRoleArn": {"Value": {"Fn::GetAtt": ["ReadRole", "Arn"]}}, "ActRoleArn": {"Value": {"Fn::GetAtt": ["ActRole", "Arn"]}}},
    }
    terraform = (
        f'# Compass installation {install_id}: the read role and the act role (compass connect template)\n'
        f'resource "aws_iam_role" "compass_read" {{\n  name = "{read_role}"\n  path = "{ROLE_PATH}"\n'
        f'  assume_role_policy = jsonencode({json.dumps(trust, indent=2)})\n  tags = {{ "{TAG}" = "{prefix}" }}\n}}\n\n'
        f'resource "aws_iam_role_policy" "compass_read" {{\n  name   = "{READ_POLICY_NAME}"\n  role   = aws_iam_role.compass_read.id\n'
        f'  policy = jsonencode({json.dumps(read_policy, indent=2)})\n}}\n\n'
        f'resource "aws_iam_role" "compass_act" {{\n  name = "{act_role}"\n  path = "{ROLE_PATH}"\n'
        f'  assume_role_policy = jsonencode({json.dumps(trust, indent=2)})\n  tags = {{ "{TAG}" = "{prefix}" }}\n}}\n'
    )
    out_dir = (core_config.config_path(workspace.workspace_id) or Path("config/local.json")).parent / "connect" / account
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compass-roles.yaml").write_text(yaml.safe_dump(cloudformation, sort_keys=False), encoding="utf-8")
    (out_dir / "main.tf").write_text(terraform, encoding="utf-8")
    core_config.save_workspace_config(workspace.workspace_id, config)
    region = aws_cfg["region"]
    return {
        "workspace_id": workspace.workspace_id,
        "cloudformation": str(out_dir / "compass-roles.yaml"),
        "terraform": str(out_dir / "main.tf"),
        "console": f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}#/stacks/create",
        "roles": aws_cfg["roles"],
        "external_id": external,
        "base_identity": base,
        "next": [f"create the stack in account {account} (upload compass-roles.yaml), or apply main.tf", "then `compass connect verify`"],
    }
