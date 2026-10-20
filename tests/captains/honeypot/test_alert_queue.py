"""Decoy alerts flow into the control account (docs/specs/access-model.md §6, D8): the queue
policy, the consumer on the base identity, the arm-time queue check, and the pull request
that points planted decoys at the new queues."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from captains.honeypot import alert_consumer, arm
from captains.honeypot.pr_writer import HoneypotPRWriter, parse_honeytokens_tf
from captains.honeypot.terraform_generator import generate_terraform, module_slug, root_outputs
from compass_core import sessions
from compass_core.connect import queue_policy

ACCOUNT = "123456789012"
QUEUE = "arn:aws:sqs:us-east-1:111111111111:compass-alerts"


class _Content:
    def __init__(self, path, text, sha):
        self.path, self.sha, self.type = path, sha, "file"
        self.decoded_content = text.encode()


class FakeRepo:
    """The recording fake of test_pr_writer, kept small: one branch, files by path."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.prs = []
        self._n = 0

    def get_branch(self, name):
        from types import SimpleNamespace

        return SimpleNamespace(commit=SimpleNamespace(sha="base"))

    def create_git_ref(self, ref, sha):
        pass

    def get_contents(self, path, ref=None):
        from github import GithubException

        if path in self.files:
            self._n += 1
            return _Content(path, self.files[path], f"sha{self._n}")
        children = [p for p in self.files if p.startswith(path.rstrip("/") + "/")]
        if children:
            return [_Content(p, self.files[p], "d") for p in children]
        raise GithubException(404, {"message": "Not Found"}, {})

    def create_file(self, path, msg, content, branch=None):
        self.files[path] = content

    def update_file(self, path, msg, content, sha, branch=None):
        self.files[path] = content

    def delete_file(self, path, msg, sha, branch=None):
        del self.files[path]

    def create_pull(self, title, body, head, base):
        from types import SimpleNamespace

        self.prs.append({"title": title, "body": body, "head": head})
        return SimpleNamespace(html_url="https://github.com/o/r/pull/1")


def _token(name="svc_backup", cls="aws_credential", hid="ht-1"):
    return {"honeytoken_id": hid, "honeytoken_class": cls, "name": name, "workspace_id": "ws-1"}


def _plant(writer, token):
    return writer.create_plant_pr(honeytoken=token, hcl_files=generate_terraform(token), resolved_module_path=None,
                                  module_slug=module_slug(token), queue_arn=QUEUE, workspace_id="ws-1", root_outputs=root_outputs(token))
RUNTIME = f"arn:aws:iam::{ACCOUNT}:user/service/svc-compass-runtime-k3f9a2"
NEW_QUEUE = f"arn:aws:sqs:us-east-1:{ACCOUNT}:compass-decoy-alerts-local"


def test_the_queue_policy_lets_the_target_account_send_and_the_base_identity_read():
    policy = queue_policy(NEW_QUEUE, target_account="210987654321", base_identity_arn=RUNTIME)
    sids = {s["Sid"]: s for s in policy["Statement"]}
    assert sids["TargetAccountDelivery"]["Principal"] == {"AWS": "arn:aws:iam::210987654321:root"} and sids["TargetAccountDelivery"]["Action"] == "sqs:SendMessage"
    assert sids["DecoyEventBridgeRules"]["Condition"] == {"StringEquals": {"aws:SourceAccount": "210987654321"}}
    assert sids["BaseIdentityReads"]["Principal"] == {"AWS": RUNTIME} and "sqs:SendMessage" not in sids["BaseIdentityReads"]["Action"]
    assert all(s["Resource"] == NEW_QUEUE for s in policy["Statement"])


def test_the_consumer_polls_with_the_base_identity(monkeypatch):
    calls = []

    class FakeSession:
        def client(self, name, **kw):
            calls.append((name, kw))
            return "sqs-client"

    monkeypatch.setattr(sessions, "control", lambda region=None: calls.append(("control", region)) or FakeSession())
    consumer = alert_consumer.AlertConsumer(queue_url=f"https://sqs.eu-west-1.amazonaws.com/{ACCOUNT}/compass-decoy-alerts-local")
    assert consumer._sqs == "sqs-client" and ("control", "eu-west-1") in calls, "the queue's own region, on the base identity"


@mock_aws
def test_arming_checks_that_the_queue_accepts_the_target_account(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    sessions.clear_cache()
    sqs = boto3.client("sqs")
    url = sqs.create_queue(QueueName="compass-decoy-alerts-local")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    config = {"aws": {"account_id": "210987654321", "region": "us-east-1",
                      "control": {"alert_queues": {"us-east-1": {"arn": arn, "url": url}}}}}
    sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps(queue_policy(arn, target_account="210987654321", base_identity_arn=RUNTIME))})
    ok = arm._check_alert_queue(config, "us-east-1")
    assert ok["ok"] and ok["name"] == "alert_queue" and "210987654321" in ok["detail"]
    sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps(queue_policy(arn, target_account=ACCOUNT, base_identity_arn=RUNTIME))})
    wrong = arm._check_alert_queue(config, "us-east-1")
    assert not wrong["ok"] and "compass connect" in wrong["detail"]
    elsewhere = arm._check_alert_queue(config, "eu-west-1")
    assert not elsewhere["ok"] and "regions=eu-west-1" in elsewhere["detail"], "EventBridge cannot deliver across regions"
    assert arm._check_alert_queue({"aws": {}}, "us-east-1") is None

    # the check rides along with the token's own checks when the config is given
    class Broken:
        def __getattr__(self, name):
            def _call(*a, **k):
                raise RuntimeError("no AWS here")

            return _call

    class Sess:
        def client(self, *a, **k):
            return Broken()

    sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps(queue_policy(arn, target_account="210987654321", base_identity_arn=RUNTIME))})
    with_config = arm.verify_honeytoken(Sess(), {"honeytoken_class": "aws_credential", "honeytoken_id": "t", "name": "n", "region": "us-east-1"}, None, config=config)
    assert any(c["name"] == "alert_queue" and c["ok"] for c in with_config["checks"])


def test_replant_alerts_rewrites_only_the_queue_inputs(monkeypatch):
    from captains.honeypot import pr_writer

    monkeypatch.setattr(pr_writer, "resolve_github_token", lambda *a, **k: "t")
    repo = FakeRepo({"main.tf": "# customer file\n"})
    monkeypatch.setattr(pr_writer, "github_client", lambda token: type("G", (), {"get_repo": lambda self, name: repo})())
    writer = HoneypotPRWriter({"github": {"honeypot_repo": "o/r"}})
    _plant(writer, _token())
    _plant(writer, _token(name="svc_reporting", hid="ht-2"))
    before = parse_honeytokens_tf(repo.files["services.tf"])
    assert before.modules["svc_svc_backup"]["alert_queue_arn"] == f'"{QUEUE}"'

    out = writer.create_replant_alerts_pr(queues_by_region={"us-east-1": NEW_QUEUE, "eu-west-1": NEW_QUEUE.replace("us-east-1", "eu-west-1")})
    assert out["pr_url"] == "https://github.com/o/r/pull/1"
    assert sorted(r["module"] for r in out["rewritten"]) == ["svc_account", "svc_svc_backup", "svc_svc_reporting"]
    assert all(r["from"] == QUEUE and r["to"] == NEW_QUEUE for r in out["rewritten"]), "the old us-east-1 queue becomes the new us-east-1 one"
    after = parse_honeytokens_tf(repo.files["services.tf"])
    assert after.modules["svc_svc_backup"]["alert_queue_arn"] == f'"{NEW_QUEUE}"'
    assert after.modules["svc_svc_backup"]["alert_delivery_role_arn"] == before.modules["svc_svc_backup"]["alert_delivery_role_arn"], "nothing else changes"
    assert after.outputs == before.outputs
    assert repo.prs[-1]["head"].endswith("replant-alerts") and "alert_queue_arn" in repo.prs[-1]["body"]

    again = writer.create_replant_alerts_pr(queues_by_region={"us-east-1": NEW_QUEUE})
    assert again["pr_url"] is None and "already" in again["reason"]
    empty = HoneypotPRWriter({"github": {"honeypot_repo": "o/r"}})
    monkeypatch.setattr(pr_writer, "github_client", lambda token: type("G", (), {"get_repo": lambda self, name: FakeRepo({})})())
    assert empty.create_replant_alerts_pr(queues_by_region={"us-east-1": NEW_QUEUE})["reason"].endswith("nothing planted yet")


def test_a_plant_takes_the_queue_of_its_region():
    from captains.honeypot.captain import HoneypotCaptain

    config = {"workspace_id": "ws", "org_id": "ws", "aws": {"region": "us-east-1", "control": {"alert_queues": {
        "us-east-1": {"arn": NEW_QUEUE, "url": "u"}, "eu-west-1": {"arn": NEW_QUEUE.replace("us-east-1", "eu-west-1"), "url": "v"}}}},
        "honeypot": {"alert_queue_arn": "arn:aws:sqs:us-east-1:111111111111:old"}}
    captain = HoneypotCaptain(config=config, workspace_id="ws")
    assert captain._alert_queue_arn({"region": "eu-west-1"}) == NEW_QUEUE.replace("us-east-1", "eu-west-1")
    assert captain._alert_queue_arn({}) == NEW_QUEUE, "the default region's queue"
    assert captain._alert_queue_arn({"region": "ap-south-1"}) == NEW_QUEUE, "no queue there yet: the default, with a warning"
    legacy = HoneypotCaptain(config={"workspace_id": "ws", "org_id": "ws", "aws": {"region": "us-east-1"}, "honeypot": {"alert_queue_arn": "arn:aws:sqs:us-east-1:111111111111:old"}}, workspace_id="ws")
    assert legacy._alert_queue_arn({}) == "arn:aws:sqs:us-east-1:111111111111:old", "an install before connect keeps its queue"
    with pytest.raises(RuntimeError, match="compass connect"):
        HoneypotCaptain(config={"workspace_id": "ws", "org_id": "ws", "aws": {}}, workspace_id="ws")._alert_queue_arn({})
