"""One broker for every AWS session Compass makes (docs/specs/access-model.md §7).

``control()`` is the base identity: the credentials the containers run with, for
the control account's own resources (DynamoDB, the alert queues, Secrets Manager,
SES) and for ``sts:AssumeRole``. Off AWS it is the runtime user ``compass connect``
created, whose key is in the config (``aws.control.credentials``); on AWS it is
the task role or instance profile. ``target(capability)`` is a session in the
target account: the read role or the act role assumed from the base identity,
named ``<prefix>-<captain>-<job>``, narrowed by an inline session policy to what
the captain's manifest declares (``compass_core.permissions``), and carrying the
person who asked as ``SourceIdentity``. The target account's CloudTrail then
answers "which captain, which job, asked by whom" without Compass's own logs.

There is no other way to a target account: no profile, no static key, no
ambient credentials. ``profile()`` is the one exception, for ``connect`` itself,
which runs once with the operator's profile mounted and creates the roles.

Nothing else constructs a boto3 session: ``tests/compass_core/test_sessions_only.py``
fails the build on ``boto3.Session(``, ``boto3.client(`` or ``boto3.resource(``
outside this module. The captain comes from a context variable that
``dispatch`` sets for the handler's duration and the API's session middleware
sets for a request; Compass's own calls run as ``core``. A host that brokers
credentials differently installs its own factory with ``set_session_factory``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Literal, Mapping, Optional, Sequence, Tuple

import boto3

from compass_core import events
from compass_core.aws import _aws_config, control_config, external_id, resolve_target_region, role_arn

logger = logging.getLogger("compass.sessions")

Capability = Literal["read", "write", "act"]
CAPABILITIES = ("read", "write", "act")
CORE = "core"                      # Compass's own calls: the settings page, connect, the self-audit
SCHEDULER = "scheduler"            # SourceIdentity of a session nobody asked for in person
DEFAULT_PREFIX = "compass"         # aws.session_prefix; a cover name, like the responder role's
REFRESH_MARGIN = 300               # seconds before expiry at which a cached read session is renewed

_NAME_RE = re.compile(r"^[\w+=,.@-]{2,64}$")       # STS's RoleSessionName and SourceIdentity
_BAD_CHARS = re.compile(r"[^\w+=,.@-]")

_captain: ContextVar[Optional[str]] = ContextVar("compass_session_captain", default=None)
_requester: ContextVar[Optional[str]] = ContextVar("compass_session_requester", default=None)
_request_id: ContextVar[Optional[str]] = ContextVar("compass_session_request_id", default=None)


# ── who is asking ─────────────────────────────────────────────────────────────

@contextmanager
def captain_context(captain: str, *, requester: Optional[str] = None, request_id: Optional[str] = None) -> Iterator[None]:
    """Run a block as ``captain``: what ``dispatch`` does around a handler and the API's
    session middleware around a request. ``requester`` is the person who asked (the
    session's ``SourceIdentity``); ``request_id`` names in-request sessions ``api-<id>``."""
    tokens: List[Tuple[ContextVar, Token]] = [(_captain, _captain.set(captain))]
    if requester is not None:
        tokens.append((_requester, _requester.set(requester)))
    if request_id is not None:
        tokens.append((_request_id, _request_id.set(request_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_captain() -> Optional[str]:
    return _captain.get()


def current_requester() -> Optional[str]:
    return _requester.get()


def current_request_id() -> Optional[str]:
    return _request_id.get()


# ── what a session is ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionRequest:
    """Everything the factory needs to build one target session."""

    capability: str
    captain: str
    job: str
    region: Optional[str]
    config: Mapping[str, Any]
    requester: Optional[str]            # the person who asked; None → SCHEDULER
    action: Any = None                  # act only: the one action this session may perform (grants, later)


@dataclass(frozen=True)
class SessionInfo:
    """What the audit row of an action records about the session that ran it (``session_info``)."""

    name: str
    capability: str
    captain: str
    job: str
    mode: str                           # assume_role (the only mode; kept for the audit row)
    role_arn: Optional[str] = None
    source_identity: Optional[str] = None
    policy_hash: Optional[str] = None   # sha256 of the session policy the role was narrowed with


SessionFactory = Callable[[SessionRequest], Any]
_factory: Optional[SessionFactory] = None


def set_session_factory(factory: Optional[SessionFactory]) -> None:
    """The seam: a host that brokers target credentials differently (a worker that asks
    the daemon for them, a product with its own vault) installs its own. None restores
    the open default."""
    global _factory
    _factory = factory


def session_info(session: Any) -> Optional[SessionInfo]:
    return getattr(session, "_compass_session", None)


def _tag(session: Any, info: SessionInfo) -> Any:
    session._compass_session = info
    return session


# ── the control side ──────────────────────────────────────────────────────────

_control_lock = threading.Lock()
_control_cached: Tuple[Any, Dict[str, Any], float] = (None, {}, 0.0)
CONTROL_TTL = 60.0


def _control_credentials() -> Dict[str, Any]:
    """``aws.control.credentials`` of the default workspace, re-read once a minute so a
    rotated key is picked up without a restart. Empty when there is no config yet or
    the base identity is the machine's own role (``credential_source: ambient``)."""
    global _control_cached
    key = (os.getenv("COMPASS_CONFIG") or "", os.getenv("DEFAULT_WORKSPACE") or os.getenv("TARGET") or "")
    with _control_lock:
        cached_key, cached, at = _control_cached
        if cached_key == key and time.time() - at < CONTROL_TTL:
            return cached
    found: Dict[str, Any] = {}
    try:
        from compass_core.config import workspace_config
        from compass_core.workspace import default_workspace_id

        workspace_id = default_workspace_id()
        control = control_config(workspace_config(workspace_id)) if workspace_id else {}
        creds = control.get("credentials") if control.get("credential_source", "access_key") == "access_key" else None
        if isinstance(creds, dict) and creds.get("access_key_id") and creds.get("secret_access_key"):
            found = {"aws_access_key_id": str(creds["access_key_id"]), "aws_secret_access_key": str(creds["secret_access_key"])}
    except Exception as exc:  # noqa: BLE001 - no workspace yet is normal before init_config
        logger.debug("no control credentials in the config: %s", exc)
    with _control_lock:
        _control_cached = (key, found, time.time())
    return found


def control(*, region: Optional[str] = None) -> boto3.Session:
    """The base identity, for the control account's own resources only: AssumeRole,
    the alert queues, DynamoDB, Secrets Manager, SES. Never for a target account."""
    return boto3.Session(region_name=region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None, **_control_credentials())


def profile(*, region: Optional[str] = None) -> boto3.Session:
    """The operator's profile: the ambient credentials, whatever the config says. For
    ``compass connect`` only, which runs once with ``~/.aws`` mounted and creates the
    roles; nothing that runs as a service calls this."""
    return boto3.Session(region_name=region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None)


class RuntimeIdentityError(RuntimeError):
    """The daemon or the API is being run the way `connect` is: with a profile."""


def assert_runtime(config: Mapping[str, Any]) -> Optional[str]:
    """What `serve` and `api` check once at start (the spec's §4): no profile in the
    environment, no ~/.aws in the container, and the ambient identity is the runtime
    identity `connect` recorded. Returns the identity's ARN, or None when there is no
    target yet (a fresh install shows the Connect card and runs nothing that needs
    one). Off unless COMPASS_RUNTIME_CHECK is set, which the image sets: a checkout
    on a laptop has ~/.aws for good reasons."""
    if (os.getenv("COMPASS_RUNTIME_CHECK") or "").strip().lower() not in ("1", "true", "yes"):
        return None
    if os.getenv("AWS_PROFILE"):
        raise RuntimeIdentityError("AWS_PROFILE is set: the profile is for `compass connect` only; remove it from .env and the compose environment")
    home_aws = os.path.join(os.path.expanduser("~"), ".aws")
    if os.path.isdir(home_aws):
        raise RuntimeIdentityError(f"{home_aws} is mounted: the profile is for `compass connect` only; remove the ~/.aws volume from docker-compose.yml")
    control_cfg = control_config(config)
    expected = str(control_cfg.get("runtime_arn") or "")
    if not role_arn(config, "read") or not expected:
        return None
    arn = str(control().client("sts").get_caller_identity()["Arn"])
    if not _same_identity(arn, expected):
        raise RuntimeIdentityError(f"running as {arn}, not the runtime identity `connect` created ({expected}); run `compass connect` again or fix the credentials")
    return arn


def _same_identity(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    # a role shows up as arn:aws:sts::<acct>:assumed-role/<name>/<session>; the config holds arn:aws:iam::<acct>:role[/path]/<name>
    if ":assumed-role/" in actual and ":role/" in expected:
        return actual.split(":assumed-role/")[1].split("/")[0] == expected.split(":role/")[1].rsplit("/", 1)[-1] and actual.split(":")[4] == expected.split(":")[4]
    return False


# ── the target side ───────────────────────────────────────────────────────────

def target(
    capability: str,
    *,
    region: Optional[str] = None,
    captain: Optional[str] = None,
    job_id: Optional[str] = None,
    approver: Optional[str] = None,
    action: Any = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Any:
    """A session in the target account, named and attributed.

    ``capability`` is ``read``, ``write`` or ``act`` (``respond`` is accepted for ``act``).
    The captain is the argument, else the one ``dispatch`` or the API set; a session is
    never anonymous. The job is the argument, else the running job, else the API
    request (``api-<id>``), else a one-off id for a command run by hand. ``approver``
    is the person whose approval runs an action; it becomes the ``SourceIdentity``.
    ``config`` defaults to the running job's workspace config. Returns None when the
    config names no role for the capability: `compass connect` has not run.
    """
    cap = _capability(capability)
    name = captain or current_captain()
    if not name:
        raise ValueError(
            "no captain for this AWS session: run it through dispatch or the API, or pass captain= (sessions.CORE for Compass's own calls)"
        )
    cfg = config if config is not None else _current_config()
    request = SessionRequest(
        capability=cap,
        captain=name,
        job=_job(job_id),
        region=region,
        config=cfg,
        requester=approver or current_requester(),
        action=action,
    )
    return (_factory or _build)(request)


def target_env(capability: str, *, region: Optional[str] = None, **kw: Any) -> Dict[str, Optional[str]]:
    """``target(...)`` as environment variables for a subprocess.

    Tools that are not boto3 (terraform, in particular) cannot be handed a session.
    They read the standard AWS environment variables instead, so the session's
    credentials are frozen and passed that way. ``AWS_PROFILE`` maps to None so the
    caller can *remove* it: left in place it would silently win over these variables
    and point terraform back at the control account.
    """
    session = target(capability, region=region, **kw)
    if session is None:
        return {}
    credentials = session.get_credentials()
    if credentials is None:
        return {}
    frozen = credentials.get_frozen_credentials()
    env: Dict[str, Optional[str]] = {
        "AWS_ACCESS_KEY_ID": frozen.access_key,
        "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        "AWS_PROFILE": None,
        "AWS_SESSION_TOKEN": frozen.token or None,
    }
    resolved_region = session.region_name or resolve_target_region(kw.get("config"), default=region)
    if resolved_region:
        env["AWS_REGION"] = resolved_region
        env["AWS_DEFAULT_REGION"] = resolved_region
    return env


def with_region(session: Any, region: str) -> Any:
    """The same credentials pinned to another region: how the mapper walks regions
    without assuming the role once per region."""
    frozen = session.get_credentials().get_frozen_credentials()
    pinned = boto3.Session(
        aws_access_key_id=frozen.access_key,
        aws_secret_access_key=frozen.secret_key,
        aws_session_token=frozen.token,
        region_name=region,
    )
    info = session_info(session)
    return _tag(pinned, info) if info else pinned


# ── names ─────────────────────────────────────────────────────────────────────

def session_name(captain: str, job: str, *, prefix: Optional[str] = None) -> str:
    """``<prefix>-<captain>-<job>``, valid for STS (``[\\w+=,.@-]{2,64}``): the captain is
    cut at 30 characters and the job takes what is left."""
    head = f"{_clean(prefix or DEFAULT_PREFIX)[:16]}-{_clean(captain)[:30]}-"
    name = (head + _clean(job)[: max(64 - len(head), 8)])[:64]
    if not _NAME_RE.match(name):  # pragma: no cover - _clean leaves only valid characters
        raise ValueError(f"invalid session name {name!r}")
    return name


def _clean(value: Any) -> str:
    cleaned = re.sub(r"-{2,}", "-", _BAD_CHARS.sub("-", str(value or ""))).strip("-")
    return cleaned or "x"


def _capability(capability: str) -> str:
    cap = (capability or "").strip().lower()
    if cap == "respond":
        cap = "act"
    if cap not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}; one of {CAPABILITIES}")
    return cap


def _job(job_id: Optional[str]) -> str:
    running = job_id or events.current_job_id()
    if running:
        return _clean(running)
    request_id = current_request_id()
    if request_id:
        return "api-" + _clean(request_id)[:8]
    return "cli-" + uuid.uuid4().hex[:8]


def _current_config() -> Mapping[str, Any]:
    workspace_id = events.current_workspace_id()
    if not workspace_id:
        raise ValueError("no workspace config in context: pass config=")
    from compass_core.config import workspace_config

    return workspace_config(workspace_id)


# ── the open default: assume the role, narrowed to the captain's declaration ──

def _build(request: SessionRequest) -> Any:
    config = request.config
    arn = role_arn(config, request.capability)
    if not arn:
        return None
    region = resolve_target_region(config, default=request.region)
    aws_cfg = _aws_config(config)
    name = session_name(request.captain, request.job, prefix=aws_cfg.get("session_prefix"))
    source_identity = _source_identity(aws_cfg, request)
    policy, policy_arns = _narrowing(request)
    creds = _assumed_credentials(request, role_arn=arn, external_id=external_id(config), name=name, source_identity=source_identity,
                                 region=region, policy=policy, policy_arns=policy_arns)
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    digest = hashlib.sha256((policy or "".join(policy_arns)).encode()).hexdigest()[:16] if (policy or policy_arns) else None
    return _tag(session, SessionInfo(name=name, capability=request.capability, captain=request.captain, job=request.job,
                                     mode="assume_role", role_arn=arn, source_identity=source_identity, policy_hash=digest))


def _narrowing(request: SessionRequest) -> Tuple[Optional[str], List[str]]:
    """The session policy: a read session gets the captain's declared reads (inline, or
    the captain's managed policy when the declaration is too large to pass inline); an
    act session gets the one action it runs (the grant layer fills ``request.action``)."""
    from compass_core import captains, permissions

    if request.capability == "act":
        statements = getattr(request.action, "statements", None)
        if statements:
            return json.dumps({"Version": "2012-10-17", "Statement": list(statements)}, separators=(",", ":")), []
        return None, []
    manifest = captains.manifest(request.captain)
    declared = manifest.permissions if manifest is not None else None
    policy = permissions.session_policy(declared, request.config)
    if policy is not None:
        return policy, []
    managed = (_aws_config(request.config).get("managed_policies") or {}).get(request.captain)
    if not managed:
        raise ValueError(f"{request.captain}'s read block is too large for an inline session policy and no managed policy is recorded "
                         f"(aws.managed_policies.{request.captain}); run `compass connect` again")
    return None, [str(managed)]


def _source_identity(aws_cfg: Mapping[str, Any], request: SessionRequest) -> Optional[str]:
    """Only when the config says the trust policy allows it (`connect` writes both): an
    AssumeRole that sets a SourceIdentity the role does not permit is refused outright."""
    if not aws_cfg.get("source_identity"):
        return None
    return _clean(request.requester or SCHEDULER)[:64]


# ── the credential cache: one AssumeRole per captain and job, renewed before expiry

_cache: Dict[Tuple[Any, ...], Tuple[Dict[str, Any], float]] = {}
_cache_lock = threading.Lock()


def _assumed_credentials(request: SessionRequest, *, role_arn: str, external_id: Optional[str], name: str,
                         source_identity: Optional[str], region: Optional[str], policy: Optional[str] = None,
                         policy_arns: Sequence[str] = ()) -> Dict[str, Any]:
    key = (request.captain, request.job, request.capability, role_arn, external_id or "", name, source_identity or "", policy or "", tuple(policy_arns))
    cacheable = request.capability != "act"  # an action assumes afresh: one CloudTrail entry per approval
    if cacheable:
        with _cache_lock:
            cached = _cache.get(key)
        if cached and cached[1] - time.time() > REFRESH_MARGIN:
            return cached[0]
    kwargs: Dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": name}
    if external_id:
        kwargs["ExternalId"] = external_id
    if source_identity:
        kwargs["SourceIdentity"] = source_identity
    if policy:
        kwargs["Policy"] = policy
    if policy_arns:
        kwargs["PolicyArns"] = [{"arn": arn} for arn in policy_arns]
    creds = dict(_sts_client(region).assume_role(**kwargs)["Credentials"])
    if cacheable:
        expiry = creds.get("Expiration")
        expires_at = expiry.timestamp() if hasattr(expiry, "timestamp") else time.time() + 3600
        with _cache_lock:
            _cache[key] = (creds, expires_at)
    return creds


def _sts_client(region: Optional[str]) -> Any:
    return control(region=region).client("sts")


def clear_cache() -> None:
    global _control_cached
    with _cache_lock:
        _cache.clear()
    with _control_lock:
        _control_cached = (None, {}, 0.0)
