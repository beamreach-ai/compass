"""init_config, init_alerts and serve: the setup a new install goes through."""

import json
import threading

import boto3
import pytest
from moto import mock_aws

from compass_core import commands, dispatch, queue, serve
from compass_core import config as core_config


def test_init_config_writes_a_complete_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_gh_token", lambda: "ghp_test")
    monkeypatch.setattr(commands, "_gh_login", lambda token: "octocat")
    for name in ("APP_PASSWORD", "APP_USERNAME", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    out = tmp_path / "local.json"
    result = commands.init_config({"workspace": "local", "region": "eu-west-1", "github_repo": "acme/infra", "terraform_root": "envs/prod", "out": str(out)})
    cfg = json.loads(out.read_text())
    assert cfg["aws"] == {"auth_mode": "local_iam", "region": "eu-west-1"}
    assert cfg["github"]["token"] == "ghp_test" and cfg["github"]["login"] == "octocat" and cfg["github"]["honeypot_repo"] == "acme/infra" and cfg["github"]["org"] == "acme"
    assert cfg["honeypot"]["terraform_root"] == "envs/prod"
    assert cfg["dynamodb_config"]["map"] == "compass_map_local" and cfg["dynamodb_config"]["items"] == "compass_items_local"
    assert result["config"]["github"]["token"] == "<set>" and result["github_token"] == "from gh CLI"
    assert cfg["dashboard"]["username"] == "admin" and len(cfg["dashboard"]["password"]) >= 12
    assert result["login"]["username"] == "admin" and result["login"]["password"] == cfg["dashboard"]["password"]
    assert result["config"]["dashboard"]["password"] == "<see login>"
    with pytest.raises(FileExistsError):
        commands.init_config({"workspace": "local", "out": str(out)})


@mock_aws
def test_init_alerts_creates_the_queue_with_a_policy_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"workspace_id": "local", "aws": {"auth_mode": "local_iam", "region": "us-east-1"}, "honeypot": {}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    monkeypatch.setattr(core_config, "_loader", None)
    session = boto3.Session(region_name="us-east-1")
    monkeypatch.setattr("compass_core.aws.build_target_session", lambda config, **kwargs: session)

    out = commands.init_alerts({"workspace_id": "local"})
    assert out["queue_arn"].endswith(":compass-decoy-alerts-local")
    policy = json.loads(session.client("sqs").get_queue_attributes(QueueUrl=out["queue_url"], AttributeNames=["Policy"])["Attributes"]["Policy"])
    assert {s["Sid"] for s in policy["Statement"]} == {"DecoyEventBridgeRules", "AccountPrincipals"}
    saved = json.loads(path.read_text())
    assert saved["honeypot"]["alert_queue_arn"] == out["queue_arn"] and saved["honeypot"]["alert_queue_url"] == out["queue_url"]


def test_serve_once_runs_one_prs_tick_and_reports_queues(monkeypatch):
    monkeypatch.setattr(core_config, "_loader", lambda ws: {"workspace_id": ws, "honeypot": {"alert_queue_url": "https://sqs.us-east-1.amazonaws.com/1/q"}})
    ticks = []
    monkeypatch.setattr("captains.prs.scheduler.tick", lambda workspaces, client=None: ticks.append(list(workspaces)) or 1)

    class FakeConsumer:
        instances = []

        def __init__(self, queue_url=None, **_):
            self.queue_url = queue_url
            self.stopped = threading.Event()
            FakeConsumer.instances.append(self)

        def run(self, install_signals=True):
            self.stopped.wait(5)

        def stop(self, *_):
            self.stopped.set()

    monkeypatch.setattr("captains.honeypot.alert_consumer.AlertConsumer", FakeConsumer)
    previous = queue._factory
    try:
        result = serve.serve({"workspace_id": "ws-1", "once": True, "prs_tick": 300})
    finally:
        queue.set_job_queue(previous)
    assert ticks == [["ws-1"]] and result["ticks"] == 1
    assert result["queues"] == ["https://sqs.us-east-1.amazonaws.com/1/q"]
    assert FakeConsumer.instances[0].stopped.is_set(), "serve stops its consumers on exit"
    assert ("honeypot", "investigate") in dispatch.commands(), "serve loads every open command table"


def test_workspace_id_comes_from_the_config_file_when_nothing_else_names_it(tmp_path, monkeypatch):
    from compass_core import workspace

    monkeypatch.delenv("DEFAULT_WORKSPACE", raising=False)
    monkeypatch.delenv("TARGET", raising=False)
    monkeypatch.delenv("COMPASS_CONFIG", raising=False)
    monkeypatch.setattr(core_config, "_loader", None)
    with pytest.raises(LookupError, match="workspace_id is required"):
        workspace.resolve({})
    path = tmp_path / "ws.json"
    path.write_text(json.dumps({"workspace_id": "from-file", "aws": {"region": "eu-west-1"}}))
    monkeypatch.setenv("COMPASS_CONFIG", str(path))
    ws = workspace.resolve({})
    assert ws.workspace_id == "from-file" and ws.config["aws"]["region"] == "eu-west-1"
    monkeypatch.setenv("DEFAULT_WORKSPACE", "explicit")
    assert workspace.default_workspace_id() == "explicit"


def test_queue_urls_fall_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("HONEYPOT_ALERT_QUEUE_URLS", "https://a/q1, https://a/q2")
    assert serve.queue_urls({}) == ["https://a/q1", "https://a/q2"]
    assert serve.queue_urls({"honeypot": {"alert_queue_urls": ["https://c/q"]}}) == ["https://c/q"]


def test_env_password_wins_and_no_password_is_a_warning(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "from-env")
    monkeypatch.setenv("APP_USERNAME", "ops")
    assert commands.dashboard_login({"dashboard": {"password": "file"}})["username"] == "ops"
    monkeypatch.delenv("APP_PASSWORD")
    assert commands.dashboard_login({"dashboard": {"username": "admin", "password": "file"}})["password"] == "file"
    assert "warning" in commands.dashboard_login({})
