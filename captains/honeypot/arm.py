"""HP-14: verify a honeytoken is really in place, and test-fire it end to end.

Verification is read-only against the target account and never creates
anything (in particular never a CloudTrail trail). Every check is reported
individually so the UI can say what is missing.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from captains.honeypot.classes import normalize_class
from captains.honeypot.terraform_generator import _cover, stealth_tag

TEST_FIRE_USER_AGENT = "compass-test-fire"
RULE_ENABLED_STATE = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
NO_TRAIL_DETAIL = (
    "no logging CloudTrail trail in this account; create one "
    "(the honeytoken cannot be detected without it)"
)


def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _cover_name(token: Dict[str, Any]) -> str:
    return _cover({"honeytoken_id": token.get("honeytoken_id"), "name": token.get("name")})


def _result(checks: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": all(c["ok"] for c in checks), "checks": checks}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _check_trail(session: Any) -> Dict[str, Any]:
    try:
        ct = session.client("cloudtrail")
        trails = ct.describe_trails(includeShadowTrails=True).get("trailList") or []
        for trail in trails:
            ident = trail.get("TrailARN") or trail.get("Name")
            try:
                if ct.get_trail_status(Name=ident).get("IsLogging"):
                    return _check("cloudtrail_logging", True, f"trail {trail.get('Name')} is logging")
            except Exception:
                continue
    except Exception as exc:
        return _check("cloudtrail_logging", False, _err(exc))
    return _check("cloudtrail_logging", False, NO_TRAIL_DETAIL)


def _verify_aws_credential(session: Any, token: Dict[str, Any]) -> Dict[str, Any]:
    cover = _cover_name(token)
    tid = str(token.get("honeytoken_id") or "")
    tag_key, tag_value = stealth_tag(tid)
    checks: List[Dict[str, Any]] = []
    iam = session.client("iam")
    key_id: Optional[str] = None

    user_ok = False
    try:
        iam.get_user(UserName=cover)
        user_ok = True
        checks.append(_check("iam_user", True, f"user {cover} exists"))
    except Exception as exc:
        checks.append(_check("iam_user", False, f"user {cover}: {_err(exc)}"))

    if user_ok:
        try:
            tags = {t["Key"]: t["Value"] for t in iam.list_user_tags(UserName=cover).get("Tags", [])}
            ok = tags.get(tag_key) == tag_value
            checks.append(_check("stealth_tag", ok, "tag matches" if ok else f"{tag_key} does not match this token"))
        except Exception as exc:
            checks.append(_check("stealth_tag", False, _err(exc)))
        try:
            keys = iam.list_access_keys(UserName=cover).get("AccessKeyMetadata", [])
            if len(keys) == 1:
                key_id = keys[0]["AccessKeyId"]
                # An inactive key cannot trip: AWS rejects it before any rule sees
                # the call. It is what a containment leaves behind; roll it back.
                active = keys[0].get("Status") == "Active"
                checks.append(_check(
                    "access_key",
                    active,
                    "one access key (Active)" if active
                    else f"the access key is {keys[0].get('Status', '?')}: roll back the deactivation to re-arm",
                ))
            else:
                checks.append(_check("access_key", False, f"expected exactly one access key, found {len(keys)}"))
        except Exception as exc:
            checks.append(_check("access_key", False, _err(exc)))

    rule_name = f"{cover}-activity"
    events = session.client("events")
    try:
        rule = events.describe_rule(Name=rule_name)
        state = rule.get("State")
        checks.append(_check(
            "rule_state", state == RULE_ENABLED_STATE,
            f"rule {rule_name} state {state}" + ("" if state == RULE_ENABLED_STATE else f"; expected {RULE_ENABLED_STATE}"),
        ))
        targets = events.list_targets_by_rule(Rule=rule_name).get("Targets", [])
        has_role = any(t.get("RoleArn") for t in targets)
        checks.append(_check("rule_target", has_role, "target has a delivery role" if has_role else "no target with a RoleArn"))
    except Exception as exc:
        checks.append(_check("rule_state", False, f"rule {rule_name}: {_err(exc)}"))

    checks.append(_check_trail(session))
    return _result(checks, access_key_id=key_id)


def _verify_db_credential(session: Any, token: Dict[str, Any]) -> Dict[str, Any]:
    cover = _cover_name(token)
    tag_key, tag_value = stealth_tag(str(token.get("honeytoken_id") or ""))
    checks: List[Dict[str, Any]] = []
    cluster: Optional[str] = None
    try:
        tagging = session.client("resourcegroupstaggingapi")
        resp = tagging.get_resources(
            TagFilters=[{"Key": tag_key, "Values": [tag_value]}],
            ResourceTypeFilters=["ecs:service"],
        )
        for res in resp.get("ResourceTagMappingList", []):
            arn = res.get("ResourceARN", "")
            parts = arn.split("/")
            if len(parts) >= 3:  # arn:...:service/<cluster>/<name>
                cluster = parts[1]
                break
    except Exception:
        pass
    ecs = session.client("ecs")
    try:
        if not cluster:
            for arn in ecs.list_clusters().get("clusterArns", []):
                name = arn.rsplit("/", 1)[-1]
                got = ecs.describe_services(cluster=name, services=[cover]).get("services", [])
                if any(s.get("status") == "ACTIVE" for s in got):
                    cluster = name
                    break
        if not cluster:
            checks.append(_check("listener_service", False, f"no ECS service named {cover} found"))
        else:
            svc = (ecs.describe_services(cluster=cluster, services=[cover]).get("services") or [None])[0]
            running = int((svc or {}).get("runningCount", 0))
            checks.append(_check(
                "listener_running", running >= 1,
                f"service {cover} in {cluster}: {running} running task(s)",
            ))
    except Exception as exc:
        checks.append(_check("listener_running", False, _err(exc)))
    checks.append(_check("dns_check", True, "skipped (private zone)"))
    return _result(checks)


def verify_honeytoken(session: Any, token: Dict[str, Any], placement: Optional[Dict[str, Any]] = None, *,
                      config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read-only verification. Returns {ok, checks[{name, ok, detail}], access_key_id?}.
    With the workspace config, also checks that the alert queue for the token's region
    accepts deliveries from the target account (the misconfiguration that would otherwise
    show at the first real trip)."""
    cls = normalize_class(token.get("honeytoken_class") or token.get("asset_class") or "")
    if cls == "aws_credential":
        result = _verify_aws_credential(session, token)
    elif cls == "db_credential":
        result = _verify_db_credential(session, token)
    else:
        return _result([_check("verification", True, f"no automated verification for {cls or 'this class'}")])
    if config is not None:
        queue_check = _check_alert_queue(config, str(token.get("region") or ""))
        if queue_check is not None:
            result["checks"].append(queue_check)
            result["ok"] = result["ok"] and queue_check["ok"]
    return result


def _check_alert_queue(config: Dict[str, Any], region: str) -> Optional[Dict[str, Any]]:
    """The queue in the control account that the rule delivers to must let the target
    account's roles send: its policy names arn:aws:iam::<target>:root (what `connect`
    writes). None when no queue is configured (the arm says so elsewhere)."""
    import json

    from compass_core import sessions
    from compass_core.aws import alert_queues, resolve_target_account_id, resolve_target_region

    queues = alert_queues(config)
    if not queues:
        return None
    region = region or resolve_target_region(config) or ""
    queue = queues.get(region) or next(iter(queues.values()))
    target = resolve_target_account_id(config) or ""
    try:
        queue_region = queue["arn"].split(":")[3] if queue.get("arn", "").count(":") >= 5 else region
        sqs = sessions.control(region=queue_region).client("sqs")
        policy = json.loads(sqs.get_queue_attributes(QueueUrl=queue["url"], AttributeNames=["Policy"])["Attributes"].get("Policy") or "{}")
    except Exception as exc:  # noqa: BLE001 - the queue is gone, or the base identity may not read it
        return _check("alert_queue", False, f"queue {queue.get('arn')}: {_err(exc)}")
    if region not in queues:
        return _check("alert_queue", False, f"no alert queue in {region}: EventBridge cannot deliver to {queue['arn']} from there; run `compass connect regions={region}`")
    allowed = any(
        "sqs:SendMessage" in (s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")])
        and s.get("Effect") == "Allow"
        and (
            (s.get("Principal") or {}).get("AWS") in (f"arn:aws:iam::{target}:root", "*")
            or ((s.get("Principal") or {}).get("Service") == "events.amazonaws.com"
                and str(((s.get("Condition") or {}).get("StringEquals") or {}).get("aws:SourceAccount") or target) == target)
        )
        for s in policy.get("Statement", [])
    )
    return _check("alert_queue", allowed,
                  f"queue {queue['arn']} accepts deliveries from account {target}" if allowed
                  else f"queue {queue['arn']} does not let account {target} send: run `compass connect` to re-apply its policy")


# ── Test fire ────────────────────────────────────────────────────────────────

def fire_aws_credential(
    access_key_id: str, secret_access_key: str, region: str, *, client_factory: Optional[Callable[..., Any]] = None
) -> None:
    """One real, read-only call with the honeytoken key, from the Compass side."""
    import boto3
    from botocore.config import Config

    factory = client_factory or boto3.client
    sts = factory(
        "sts",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
        config=Config(user_agent_extra=TEST_FIRE_USER_AGENT, retries={"max_attempts": 1}),
    )
    sts.get_caller_identity()


def find_test_alert(table: Any, pk: str, honeytoken_id: str, since_iso: str) -> Optional[Dict[str, Any]]:
    """The first alert for this token created at/after `since_iso` by the test call."""
    from boto3.dynamodb.conditions import Key

    from compass_core.keys import honeypot_alert_prefix

    resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(honeypot_alert_prefix(honeytoken_id)))
    for item in resp.get("Items", []):
        if str(item.get("processed_at") or "") >= since_iso and TEST_FIRE_USER_AGENT in str(item.get("user_agent") or ""):
            return item
    return None


def wait_for_test_alert(
    table: Any, pk: str, honeytoken_id: str, since_iso: str, *,
    timeout_s: float = 30.0, interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> Optional[Dict[str, Any]]:
    deadline = clock() + timeout_s
    while True:
        found = find_test_alert(table, pk, honeytoken_id, since_iso)
        if found:
            return found
        if clock() >= deadline:
            return None
        sleep(interval_s)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
