"""4.7 Decoys (docs/e2e.md): the alert queue, suggestions, planting every class, arming,
an alert becoming an incident with its notifications, responses in each mode, closing,
decommissioning, autopilot, endpoints and the decoy's secret.

The suite cannot run Terraform against moto, so "the operator merges and applies the
pull request" is done by the test: it reads the module Compass wrote and creates in moto
what the module declares (`apply_aws_credential`). That also checks the generator and
the arming check agree on names. An attacker using the key is a message on the alert
queue in the shape the module's EventBridge target sends (`send_alert`).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import pytest

from e2e import fixture

CLASSES = ("aws_credential", "db_credential", "ssh_key", "s3_object", "api_token", "dns_record")
QUEUE = "compass-decoy-alerts-local"


# ── helpers ─────────────────────────────────────────────────────────────────

def plant(stack, honeytoken_class: str, name: str, *, endpoints: Optional[List[str]] = None, resource_path: Optional[str] = None) -> Dict[str, Any]:
    """Create a decoy from the API (what the page's Plant does) and wait for its pull request."""
    body: Dict[str, Any] = {"honeytoken_class": honeytoken_class, "name": name, "region": fixture.REGION}
    if resource_path:
        body["resource_path"] = resource_path
    if endpoints:
        body["action_config"] = {"severity": "critical", "notification_endpoint_ids": endpoints}
    created = stack.api.post("/api/honeypots/tokens", json=body)
    assert created.status_code == 200, created.text
    token_id = created.json()["honeytoken_id"]
    job_id = stack.api.get(f"/api/honeypots/tokens/{token_id}").json()["plant_job_id"]
    job = stack.wait_job(job_id, timeout=300)
    assert job["status"] == "completed", f"plant {honeytoken_class}: {job.get('error')} {[e['message'] for e in job.get('events', [])[-4:]]}"
    token = stack.api.get(f"/api/honeypots/tokens/{token_id}").json()
    number = int(str(job["result_summary"]["pr_url"]).rsplit("/", 1)[1])
    pull = next(p for p in stack.repo()["pulls"] if p["number"] == number)
    return {"token": token, "job": job, "pull": pull, "id": token_id}


def module_text(stack, pull: Dict[str, Any], path_end: str = "/main.tf") -> str:
    owner, name = fixture.REPO.split("/")
    path = next(f["filename"] for f in pull["files"] if f["filename"].endswith(path_end) and "/_account/" not in f["filename"]
                and "/services/" in f["filename"])
    return stack.fakes.get(f"/_control/repos/{owner}/{name}/file", params={"path": path, "branch": pull["head"]}).json()["text"]


def apply_aws_credential(stack, planted: Dict[str, Any]) -> str:
    """Create in moto what the decoy's module declares (`terraform apply`); returns the access key id."""
    text = module_text(stack, planted["pull"])
    user = re.search(r'resource "aws_iam_user" "user" \{\s*name = "([^"]+)"\s*path = "([^"]+)"', text)
    tag = re.search(r'tags = \{\s*"([^"]+)" = "([^"]+)"', text)
    rule = re.search(r'resource "aws_cloudwatch_event_rule" "rule" \{\s*name\s*= "([^"]+)"[\s\S]*?state\s*= "([^"]+)"', text)
    assert user and tag and rule, "the module declares a user, its tag and a rule"
    iam, events = stack.aws_client("iam"), stack.aws_client("events")
    iam.create_user(UserName=user.group(1), Path=user.group(2), Tags=[{"Key": tag.group(1), "Value": tag.group(2)}])
    created = iam.create_access_key(UserName=user.group(1))["AccessKey"]
    key = created["AccessKeyId"]
    planted["secret"] = created["SecretAccessKey"]  # what the apply output shows; the demo's trip stores it with the token
    events.put_rule(Name=rule.group(1), State=rule.group(2), EventPattern=json.dumps({"detail-type": ["AWS API Call via CloudTrail"]}))
    events.put_targets(Rule=rule.group(1), Targets=[{"Id": "alerts", "Arn": f"arn:aws:sqs:{fixture.REGION}:{fixture.ACCOUNT}:{QUEUE}",
                                                      "RoleArn": f"arn:aws:iam::{fixture.ACCOUNT}:role/compass-alert-delivery"}])
    planted["user"] = user.group(1)
    return key


def ensure_trail(stack) -> None:
    trail = stack.aws_client("cloudtrail")
    if not trail.describe_trails().get("trailList"):
        stack.aws_client("s3").create_bucket(Bucket="acme-cloudtrail")
        trail.create_trail(Name="acme-trail", S3BucketName="acme-cloudtrail")
        trail.start_logging(Name="acme-trail")


def send_alert(stack, token_id: str, access_key_id: str, source_ip: str, event_name: str = "GetCallerIdentity", *, user: str = "decoy") -> None:
    """The message the decoy's EventBridge target puts on the queue when its key is used; the
    principal is the decoy's own user, under the /service/ path its module gives it."""
    message = {"token_id": token_id, "workspace_id": "local", "event_id": f"e2e-{time.time_ns()}", "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "event_name": event_name, "region": fixture.REGION, "access_key_id": access_key_id,
               "principal_arn": f"arn:aws:iam::{fixture.ACCOUNT}:user/service/{user}", "source_ip": source_ip, "user_agent": "aws-cli/2.15",
               "error_code": "", "vpc_endpoint_id": "", "service": "sts"}
    stack.aws_client("sqs").send_message(QueueUrl=f"{stack.moto_url}/{fixture.ACCOUNT}/{QUEUE}", MessageBody=json.dumps(message))


def incidents(stack) -> List[Dict[str, Any]]:
    body = stack.api.get("/api/honeypots/incidents").json()
    return body.get("incidents", body) if isinstance(body, dict) else body


def wait_incident(stack, source_ip: str, *, until=lambda incident: True, timeout: float = 180) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        for incident in incidents(stack):
            if incident.get("source_ip") == source_ip:
                last = stack.api.get(f"/api/honeypots/incidents/{incident['incident_id']}").json()
                if until(last):
                    return last
        time.sleep(3)
    raise AssertionError(f"no incident from {source_ip} that satisfies the check within {timeout}s; last: {json.dumps(last)[:800]}")


def armed_decoy(stack, name: str, *, endpoints: Optional[List[str]] = None) -> Dict[str, Any]:
    planted = plant(stack, "aws_credential", name, endpoints=endpoints)
    key = apply_aws_credential(stack, planted)
    ensure_trail(stack)
    owner, repo = fixture.REPO.split("/")
    stack.fakes.post(f"/_control/repos/{owner}/{repo}/pulls/{planted['pull']['number']}/merge").raise_for_status()
    armed = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/arm")
    assert armed.status_code == 200, armed.text
    return {**planted, "key": key, "user": planted["user"]}


# ── the tests ───────────────────────────────────────────────────────────────

def test_dec_1_connect_created_the_queue_and_the_daemon_consumes_it(stack, mapped):
    config = json.loads(stack.read("config/local.json"))
    queue = config["aws"]["control"]["alert_queues"][fixture.REGION]
    assert queue["arn"] == f"arn:aws:sqs:{fixture.REGION}:{fixture.ACCOUNT}:{QUEUE}"
    attributes = stack.aws_client("sqs").get_queue_attributes(QueueUrl=f"{stack.moto_url}/{fixture.ACCOUNT}/{QUEUE}", AttributeNames=["All"])
    policy = json.loads(attributes["Attributes"]["Policy"])
    assert {s["Sid"] for s in policy["Statement"]} == {"TargetAccountDelivery", "DecoyEventBridgeRules", "BaseIdentityReads"}
    assert attributes["Attributes"].get("SqsManagedSseEnabled") == "true"
    deadline = time.time() + 90
    while "consuming decoy alerts from" not in stack.compose("logs", "--no-color", "compass").stdout:
        assert time.time() < deadline, "the daemon did not start consuming the queue within 90s"
        time.sleep(5)
    replant = stack.cli("honeypot", "replant_alerts")
    assert replant["pr_url"] is None and "already" in replant["reason"] or replant["reason"].endswith("nothing planted yet"), replant


def test_dec_2_suggestions_appear_next_to_the_seeded_resources(stack, page, mapped):
    suggestions = stack.api.get("/api/honeypots/suggestions").json()
    assert suggestions, "the map should suggest at least one placement"
    assert all(s.get("recommended_class") in CLASSES for s in suggestions)
    text = json.dumps(suggestions)
    assert any(name in text for name in (fixture.SECRET, fixture.MANAGED_ROLE, fixture.MANAGED_FUNCTION, fixture.MANAGED_BUCKET))
    page.open("/workspace?tab=honeypots&view=honeytokens")
    page.get_by_text("Suggest", exact=False).first.wait_for()


def test_dec_11_a_webhook_endpoint_is_added_tested_and_deleted(stack):
    created = stack.api.post("/api/notifications/endpoints", json={"name": "ops", "type": "webhook", "webhook_url": "http://fakes:9000/hooks/ops"})
    assert created.status_code == 200, created.text
    endpoint_id = created.json().get("endpoint_id") or created.json().get("id")
    tested = stack.api.post(f"/api/notifications/endpoints/{endpoint_id}/test")
    assert tested.status_code == 200, tested.text
    assert any(h["name"] == "ops" for h in stack.fakes.get("/_control/hooks").json())
    spare = stack.api.post("/api/notifications/endpoints", json={"name": "spare", "type": "webhook", "webhook_url": "http://fakes:9000/hooks/spare"}).json()
    spare_id = spare.get("endpoint_id") or spare.get("id")
    assert stack.api.delete(f"/api/notifications/endpoints/{spare_id}").status_code == 200
    listed = stack.api.get("/api/notifications/endpoints").json()
    ids = {e.get("endpoint_id") or e.get("id") for e in (listed.get("endpoints", listed) if isinstance(listed, dict) else listed)}
    assert endpoint_id in ids and spare_id not in ids
    stack.records["endpoint_id"] = endpoint_id


def _listener_network(stack) -> None:
    """A db_credential decoy's listener runs in a VPC the workspace names (honeypot.network)."""
    ec2 = stack.aws_client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.42.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.42.1.0/24")["Subnet"]["SubnetId"]
    saved = stack.api.put("/api/captains/honeypot/config", json={"network": {"vpc_id": vpc, "subnet_ids": [subnet]},
                                                                  "listener_image": "ghcr.io/example/decoy-listener:1"})
    assert saved.status_code == 200, saved.text


@pytest.mark.parametrize("honeytoken_class", CLASSES)
def test_dec_3_planting_each_class_opens_a_pull_request_with_one_formatted_module(stack, honeytoken_class):
    name = f"e2e-{honeytoken_class.replace('_', '-')}"
    resource_path = None
    if honeytoken_class == "s3_object":
        refused = stack.api.post("/api/honeypots/tokens", json={"honeytoken_class": "s3_object", "name": "e2e-no-bucket"})
        assert refused.status_code == 400 and "needs its bucket" in refused.json()["detail"]
        resource_path = f"{fixture.MANAGED_BUCKET}/secrets/prod.env"
    if honeytoken_class == "db_credential":
        _listener_network(stack)
    planted = plant(stack, honeytoken_class, name, resource_path=resource_path)
    files = [f["filename"] for f in planted["pull"]["files"]]
    assert planted["pull"]["head"] == f"beamreach/honeypot/{name}" and planted["pull"]["state"] == "open"
    changed = [f["filename"] for f in planted["pull"]["files"] if f["status"] != "removed"]
    modules = {f.split("/services/", 1)[1].split("/", 1)[0] for f in changed if "/services/" in f and "_account" not in f}
    assert len(modules) == 1, files
    assert all(f.startswith(fixture.TERRAFORM_ROOT + "/") for f in files)
    owner, repo = fixture.REPO.split("/")
    for path in changed:
        text = stack.fakes.get(f"/_control/repos/{owner}/{repo}/file", params={"path": path, "branch": planted["pull"]["head"]}).json()["text"]
        stack.write(f"extensions/fmt-check/{honeytoken_class}/{path}", text)
    fmt = stack.compose("run", "--rm", "-T", "--no-deps", "--entrypoint", "terraform", "compass", "fmt", "-check", "-recursive", "-diff",
                        f"/compass/extensions/fmt-check/{honeytoken_class}", check=False)
    assert fmt.returncode == 0, f"terraform fmt would change:\n{fmt.stdout[-2000:]}"
    # these are not applied; close them so later tests see only their own pull requests open
    stack.fakes.post(f"/_control/repos/{owner}/{repo}/pulls/{planted['pull']['number']}/close").raise_for_status()


def test_dec_3_planting_from_the_page(stack, page):
    page.open("/workspace?tab=honeypots&view=honeytokens", wait_for="New honeytoken")
    page.get_by_role("button", name="+ New honeytoken").click()
    page.get_by_role("button", name="AWS credential").click()
    page.get_by_role("button", name="Next →").click()
    page.get_by_label("Name *").fill("e2e-from-the-page")
    page.get_by_label("Region").fill(fixture.REGION)
    page.get_by_role("button", name="Next →").click()
    page.get_by_role("button", name="Create honeytoken & open PR").click()
    deadline = time.time() + 180
    while not any(p["head"] == "beamreach/honeypot/e2e-from-the-page" for p in stack.repo()["pulls"]):
        assert time.time() < deadline, "no pull request for the decoy planted from the page"
        time.sleep(3)


def test_dec_4_arming_checks_the_applied_resources_and_says_what_is_missing(stack):
    planted = plant(stack, "aws_credential", "e2e-arm-check")
    early = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/arm")
    assert early.status_code == 409
    failed = {c["name"] for c in early.json()["detail"]["checks"] if not c["ok"]}
    assert {"iam_user", "rule_state"} <= failed
    apply_aws_credential(stack, planted)
    ensure_trail(stack)
    armed = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/arm")
    assert armed.status_code == 200, armed.text
    assert stack.api.get(f"/api/honeypots/tokens/{planted['id']}").json()["status"] == "armed"
    stack.records["arm_check"] = planted


def test_dec_5_an_alert_opens_an_incident_with_its_blast_radius_and_notifies_the_endpoint(stack, page):
    endpoint_id = stack.records.get("endpoint_id")
    assert endpoint_id, "DEC-11 creates the endpoint this decoy notifies"
    decoy = armed_decoy(stack, "e2e-alerting", endpoints=[endpoint_id])
    calls_before = len(stack.fakes.get("/_control/hooks").json())
    send_alert(stack, decoy["id"], decoy["key"], "203.0.113.5", user=decoy["user"])
    incident = wait_incident(stack, "203.0.113.5", until=lambda d: (d.get("incident") or d).get("blast_radius") is not None)
    detail = incident.get("incident") or incident
    assert detail["status"] == "open" and decoy["id"] in detail["honeytoken_ids"]
    assert (incident.get("alerts") or [{}])[0].get("source_ip", "203.0.113.5") == "203.0.113.5"
    deadline = time.time() + 60
    while len(stack.fakes.get("/_control/hooks").json()) <= calls_before:
        assert time.time() < deadline, "the endpoint was not notified"
        time.sleep(2)
    page.open("/workspace?tab=honeypots")
    page.get_by_text("203.0.113.5", exact=False).first.wait_for()
    stack.records["incident"] = detail["incident_id"]
    stack.records["alerting"] = decoy


def _apply_grants(stack) -> None:
    """A response runs only once its grant is armed: merged, applied, verified (compass_core.grants).
    The suite cannot run Terraform against moto, so "the operator applies" is writing the act
    policy the module renders into moto, then verifying, as the apply and the tick would."""
    module = stack.api.get("/api/access/module").json()
    stack.aws_client("iam").put_role_policy(RoleName=module["act_role"]["name"], PolicyName="ops", PolicyDocument=json.dumps(module["act_policy"]))
    verified = stack.api.post("/api/access/verify")
    assert verified.status_code == 200, verified.text


def _action(stack, mode: str) -> str:
    action_id = None
    for row in stack.api.get("/api/honeypots/actions").json()["actions"]:
        if row.get("action_type") == "iam_deactivate_key":
            stack.api.put(f"/api/honeypots/actions/{row['action_id']}", json={"mode": mode, "risk_max": 5, "enabled": mode != "off"}).raise_for_status()
            action_id = row["action_id"]
            break
    if action_id is None:
        # decoy users live under /service/ (the module's aws_iam_user path): the action may act on them and only them
        created = stack.api.post("/api/honeypots/actions", json={"action_type": "iam_deactivate_key", "mode": mode, "risk_max": 5,
                                                                 "scope": {"user_path_prefixes": ["/service/"]}})
        assert created.status_code == 200, created.text
        action_id = created.json()["action_id"]
    if mode != "off":
        _apply_grants(stack)
        rows = {r["action_id"]: r for r in stack.api.get("/api/honeypots/actions").json()["actions"]}
        assert rows[action_id]["armed"], rows[action_id]
    return action_id


def _key_status(stack, decoy) -> str:
    user = re.search(r'name = "([^"]+)"', module_text(stack, decoy["pull"])).group(1)
    return stack.aws_client("iam").list_access_keys(UserName=user)["AccessKeyMetadata"][0]["Status"]


def test_dec_7_responses_recommend_wait_for_approval_or_run_by_their_mode(stack):
    # off: recommended, nothing done
    _action(stack, "off")
    quiet = armed_decoy(stack, "e2e-respond-off")
    send_alert(stack, quiet["id"], quiet["key"], "203.0.113.10", user=quiet["user"])
    wait_incident(stack, "203.0.113.10", until=lambda d: (d.get("incident") or d).get("action") is not None)
    assert _key_status(stack, quiet) == "Active"

    # approve: parked until someone approves, then run
    action_id = _action(stack, "approve")
    gated = armed_decoy(stack, "e2e-respond-approve")
    send_alert(stack, gated["id"], gated["key"], "203.0.113.11", user=gated["user"])
    # wait for the parked action itself: the decoy's own name has "approve" in it, so the word is in the
    # incident from its first alert, before the daemon has decided anything
    parked = wait_incident(stack, "203.0.113.11", until=lambda d: any(
        p.get("action_id") == action_id for p in (((d.get("incident") or d).get("action") or {}).get("pending") or [])))
    assert _key_status(stack, gated) == "Active", "nothing runs before approval"
    incident_id = (parked.get("incident") or parked)["incident_id"]
    approved = stack.api.post(f"/api/honeypots/incidents/{incident_id}/approve", json={"action_id": action_id})
    assert approved.status_code == 200, approved.text
    assert _key_status(stack, gated) == "Inactive"

    # autopilot within risk_max: runs on its own
    _action(stack, "autopilot")
    auto = armed_decoy(stack, "e2e-respond-auto")
    send_alert(stack, auto["id"], auto["key"], "203.0.113.12", user=auto["user"])
    deadline = time.time() + 180
    while _key_status(stack, auto) != "Inactive":
        assert time.time() < deadline, "autopilot did not deactivate the decoy's key"
        time.sleep(3)
    _action(stack, "off")


def test_dec_8_an_incident_is_closed_its_alert_resolved_and_its_report_downloaded(stack, page):
    incident_id = stack.records["incident"]
    report = stack.api.get(f"/api/honeypots/incidents/{incident_id}/report")
    assert report.status_code == 200 and "203.0.113.5" in report.json()["markdown"]
    decoy = stack.records["alerting"]
    alerts = stack.api.get(f"/api/honeypots/tokens/{decoy['id']}/alerts").json()
    alert_list = alerts.get("alerts", alerts) if isinstance(alerts, dict) else alerts
    resolved = stack.api.post(f"/api/honeypots/tokens/{decoy['id']}/alerts/{alert_list[0]['alert_id']}/resolve")
    assert resolved.status_code == 200, resolved.text
    closed = stack.api.post(f"/api/honeypots/incidents/{incident_id}/close")
    assert closed.status_code == 200 and closed.json().get("closed_at")
    page.open("/workspace?tab=honeypots&view=history")
    page.get_by_text("203.0.113.5", exact=False).first.wait_for()


def test_dec_9_decommissioning_opens_a_removal_pull_request_and_retires_the_decoy(stack):
    decoy = stack.records["arm_check"]
    before = len(stack.repo()["pulls"])
    assert stack.api.delete(f"/api/honeypots/tokens/{decoy['id']}").status_code == 200
    deadline = time.time() + 180
    while len(stack.repo()["pulls"]) <= before:
        assert time.time() < deadline, "no decommission pull request"
        time.sleep(3)
    removal = stack.repo()["pulls"][-1]
    assert any(f["status"] == "removed" for f in removal["files"]) or "decommission" in removal["title"].lower()
    listed = stack.api.get("/api/honeypots/tokens").json()
    rows = listed.get("tokens", listed) if isinstance(listed, dict) else listed
    assert all(r["honeytoken_id"] != decoy["id"] or r["status"] == "decommissioned" for r in rows)


def test_dec_10_autopilot_plants_every_suggestion(stack, mapped):
    suggestions = stack.api.get("/api/honeypots/suggestions").json()
    started_at = time.time() * 1000
    started = stack.api.post("/api/honeypots/autopilot", json={"suggestions": suggestions})
    assert started.status_code == 200, started.text
    wanted = started.json().get("created", len(suggestions))
    assert wanted >= 1
    deadline = time.time() + 600
    while True:
        plants = [j for j in stack.api.get("/api/jobs", params={"limit": 200}).json()
                  if j["command"] == "plant" and j["created_at"] >= started_at - 1000]
        done = [j for j in plants if j["status"] in ("completed", "failed")]
        if len(plants) >= wanted and len(done) == len(plants):
            break
        assert time.time() < deadline, f"{len(done)} of {wanted} autopilot plants finished"
        time.sleep(5)
    failed = [j.get("error") for j in plants if j["status"] != "completed"]
    assert not failed, failed
    assert all(str((j.get("result_summary") or {}).get("pr_url", "")).startswith("https://") for j in plants)


def _contents(value: Any) -> List[str]:
    """Every `content` string in the material response (the file a decoy plants)."""
    if isinstance(value, dict):
        found = [value["content"]] if isinstance(value.get("content"), str) else []
        return found + [c for v in value.values() for c in _contents(v)]
    if isinstance(value, list):
        return [c for v in value for c in _contents(v)]
    return []


def test_dec_12_a_decoy_s_secret_is_shown_on_request_and_never_in_a_list(stack):
    decoy = stack.records["alerting"]
    material = stack.api.get(f"/api/honeypots/tokens/{decoy['id']}/material")
    assert material.status_code == 200
    secrets = [line.split("=", 1)[1].strip().strip('"') for text in _contents(material.json()) for line in text.splitlines()
               if "=" in line and len(line.split("=", 1)[1].strip()) >= 16]
    assert secrets, f"the material carries the decoy's secret: {json.dumps(material.json())[:300]}"
    listing = stack.api.get("/api/honeypots/tokens").text + stack.api.get(f"/api/honeypots/tokens/{decoy['id']}").text
    assert not [s for s in secrets if s in listing], "a secret in the list or the detail"
