"""One broker for every AWS session Compass makes (docs/specs/access-model.md §7).

``control()`` is the base identity: the credentials the containers run with,
for the control account's own resources (DynamoDB, the alert queues, Secrets
Manager, SES) and for ``sts:AssumeRole``. ``target(capability)`` is a session
in the target account, named for the captain and the job that asked for it
(``<prefix>-<captain>-<job>``) and, once the role's trust policy allows
``sts:SetSourceIdentity`` (``aws.source_identity: true``), carrying the person
who asked as ``SourceIdentity``. The target account's CloudTrail then answers
"which captain, which job, asked by whom" without Compass's own logs.

Nothing else constructs a boto3 session: ``tests/compass_core/test_sessions_only.py``
fails the build on ``boto3.Session(``, ``boto3.client(`` or ``boto3.resource(``
outside this module. The captain comes from a context variable that
``dispatch`` sets for the handler's duration and the API's session middleware
sets for a request; Compass's own calls run as ``core``. A host that brokers
credentials differently installs its own factory with ``set_session_factory``.

Until ``compass connect`` lands (the spec's §4), a target session is built the
way it always was: static keys, ``assume_role`` per capability, or the ambient
credentials in ``local_iam`` mode. Only the names are new.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Literal, Mapping, Optional, Tuple

import boto3

from compass_core import events
from compass_core.aws import (
    _aws_config,
    _local_iam_mode,
    _local_iam_runtime_enabled,
    resolve_assume_role_config,
    resolve_static_credentials_config,
    resolve_target_region,
)

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
    allow_ambient: bool = False         # validating a proposed local_iam config: the ambient credentials are the answer


@dataclass(frozen=True)
class SessionInfo:
    """What the audit row of an action records about the session that ran it (``session_info``)."""

    name: str
    capability: str
    captain: str
    job: str
    mode: str                           # assume_role | static | ambient
    role_arn: Optional[str] = None
    source_identity: Optional[str] = None
    policy_hash: Optional[str] = None   # the session policy, once captains declare what they read


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

def control(*, region: Optional[str] = None) -> boto3.Session:
    """The base identity, for the control account's own resources only: AssumeRole,
    the alert queues, DynamoDB, Secrets Manager, SES. Never for a target account."""
    return boto3.Session(region_name=region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None)


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
    allow_ambient: bool = False,
) -> Any:
    """A session in the target account, named and attributed.

    ``capability`` is ``read``, ``write`` or ``act`` (``respond`` is accepted for ``act``).
    The captain is the argument, else the one ``dispatch`` or the API set; a session is
    never anonymous. The job is the argument, else the running job, else the API
    request (``api-<id>``), else a one-off id for a command run by hand. ``approver``
    is the person whose approval runs an action; it becomes the ``SourceIdentity``.
    ``config`` defaults to the running job's workspace config. Returns None when the
    config names no target credentials, as before.
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
        allow_ambient=allow_ambient,
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


# ── the open default: how the config has always been read ─────────────────────

def _build(request: SessionRequest) -> Any:
    config = request.config
    legacy = "respond" if request.capability == "act" else request.capability  # the config keys keep their old names
    region = resolve_target_region(config, default=request.region)
    aws_cfg = _aws_config(config)

    static = resolve_static_credentials_config(config, legacy)
    if static:
        session = boto3.Session(
            aws_access_key_id=static.get("access_key_id") or static.get("aws_access_key_id"),
            aws_secret_access_key=static.get("secret_access_key") or static.get("aws_secret_access_key"),
            aws_session_token=static.get("session_token") or static.get("aws_session_token"),
            region_name=region,
        )
        return _tag(session, _info(request, mode="static", name=session_name(request.captain, request.job, prefix=aws_cfg.get("session_prefix"))))

    assume = resolve_assume_role_config(config, legacy)
    if assume:
        account_id = assume.get("account_id")
        role_name = assume.get("role_name")
        if not account_id or not role_name:
            raise KeyError("assume_role config requires account_id and role_name")
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        name = assume.get("session_name") or session_name(request.captain, request.job, prefix=aws_cfg.get("session_prefix"))
        source_identity = _source_identity(aws_cfg, request)
        creds = _assumed_credentials(request, role_arn=role_arn, external_id=assume.get("external_id"), name=name,
                                     source_identity=source_identity, region=region)
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
        return _tag(session, _info(request, mode="assume_role", name=name, role_arn=role_arn, source_identity=source_identity))

    if request.allow_ambient or (_local_iam_mode(config) and _local_iam_runtime_enabled()):
        session = boto3.Session(region_name=region)
        return _tag(session, _info(request, mode="ambient", name=session_name(request.captain, request.job, prefix=aws_cfg.get("session_prefix"))))
    return None


def _info(request: SessionRequest, *, mode: str, name: str, role_arn: Optional[str] = None, source_identity: Optional[str] = None) -> SessionInfo:
    return SessionInfo(name=name, capability=request.capability, captain=request.captain, job=request.job, mode=mode,
                       role_arn=role_arn, source_identity=source_identity)


def _source_identity(aws_cfg: Mapping[str, Any], request: SessionRequest) -> Optional[str]:
    """Only when the config says the trust policy allows it: an AssumeRole that sets a
    SourceIdentity the role does not permit is refused outright."""
    if not aws_cfg.get("source_identity"):
        return None
    return _clean(request.requester or SCHEDULER)[:64]


# ── the credential cache: one AssumeRole per captain and job, renewed before expiry

_cache: Dict[Tuple[Any, ...], Tuple[Dict[str, Any], float]] = {}
_cache_lock = threading.Lock()


def _assumed_credentials(request: SessionRequest, *, role_arn: str, external_id: Optional[str], name: str,
                         source_identity: Optional[str], region: Optional[str]) -> Dict[str, Any]:
    key = (request.captain, request.job, request.capability, role_arn, external_id or "", name, source_identity or "")
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
    with _cache_lock:
        _cache.clear()
