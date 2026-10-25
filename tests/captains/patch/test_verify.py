"""Verification (docs/specs/patching-captain.md §3.6, §6): the canary action's statements, scope,
snapshot, execute, verify and rollback against a fake ECS; refused when the grant is not armed;
skipped without a twin or an image; CI and the build read from the watcher's record; the build
workflow added when the repository has none; the whole step per pull request."""

from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from captains.patch import actions, verify
from captains.patch.findings import FindingStore
from compass_core.keys import workspace_pk

PK = workspace_pk("ws-a")
REGION, ACCOUNT = "us-east-1", "123456789012"
PROD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/prod/web"
STAGING = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/staging/web-staging"
TD_OLD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/web-staging:7"
TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/web-staging/abc"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web:pr-abc1234"
TWIN = {"staging_service": "web-staging", "cluster": "staging", "probe_paths": ["/health"], "minutes": 1}
GRANT = {"action_id": "act-1", "captain": "patch", "action_type": "patch.canary_deploy", "grant_status": "armed", "enabled": True,
         "scope": {"service_arns": [STAGING], "task_role_arns": [f"arn:aws:iam::{ACCOUNT}:role/web-task"]}}


class FakeEcs:
    """A staging service on revision 7; register and update move it; deployments settle on the next describe."""

    def __init__(self):
        self.revision = 7
        self.current = TD_OLD
        self.definitions = {TD_OLD: {"taskDefinitionArn": TD_OLD, "family": "web-staging", "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/web-task",
                                     "containerDefinitions": [{"name": "web", "image": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web:41"}, {"name": "proxy", "image": "nginx:1.27"}],
                                     "cpu": "512", "memory": "1024", "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"]}}
        self.updates: list = []
        self.describes = 0

    def describe_services(self, cluster, services):
        self.describes += 1
        return {"services": [{"serviceName": "web-staging", "serviceArn": STAGING, "taskDefinition": self.current, "desiredCount": 1, "runningCount": 1,
                              "loadBalancers": [{"targetGroupArn": TG, "containerName": "web", "containerPort": 8080}],
                              "deployments": [{"status": "PRIMARY", "taskDefinition": self.current, "rolloutState": "COMPLETED"}]}]}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": dict(self.definitions[taskDefinition])}

    def register_task_definition(self, **request):
        self.revision += 1
        arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/web-staging:{self.revision}"
        assert "taskDefinitionArn" not in request and request["family"] == "web-staging" and request["taskRoleArn"].endswith("web-task")
        self.definitions[arn] = {"taskDefinitionArn": arn, **request}
        return {"taskDefinition": {"taskDefinitionArn": arn}}

    def update_service(self, cluster, service, taskDefinition, forceNewDeployment=False):
        assert service == STAGING and cluster.endswith("cluster/staging")
        self.updates.append(taskDefinition)
        self.current = taskDefinition
        return {"service": {"taskDefinition": taskDefinition}}


class FakeElb:
    def describe_target_groups(self, TargetGroupArns):
        return {"TargetGroups": [{"TargetGroupArn": TG, "Protocol": "HTTP", "HealthCheckPath": "/", "LoadBalancerArns": [f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/staging/x"]}]}

    def describe_load_balancers(self, LoadBalancerArns):
        return {"LoadBalancers": [{"DNSName": "staging-123.us-east-1.elb.amazonaws.com"}]}


class FakeCloudWatch:
    def __init__(self, before=0, during=0):
        self.values = {"before": before, "during": during}
        self.calls = 0

    def get_metric_data(self, StartTime, EndTime, MetricDataQueries):
        self.calls += 1
        label = "before" if self.calls == 1 else "during"
        return {"MetricDataResults": [{"Id": "errors", "Values": [self.values[label]]}, {"Id": "p95", "Values": [0.2]}]}


class FakeSession:
    def __init__(self, ecs=None, cloudwatch=None):
        self.ecs = ecs or FakeEcs()
        self.cloudwatch = cloudwatch or FakeCloudWatch()

    def client(self, name, region_name=None):
        return {"ecs": self.ecs, "elbv2": FakeElb(), "cloudwatch": self.cloudwatch}[name]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_the_statements_name_the_staging_services_and_the_scope_refuses_anything_else():
    action = actions.CanaryDeploy()
    statements = action.iam_statements(actions.CanaryScope.from_dict(GRANT["scope"]), account_id=ACCOUNT, region=REGION)
    by_sid = {s["Sid"]: s for s in statements}
    assert by_sid["UpdateScopedStagingServices"]["Resource"] == [STAGING] and by_sid["UpdateScopedStagingServices"]["Action"] == ["ecs:UpdateService", "ecs:DescribeServices"]
    assert by_sid["ReadTasksOfScopedClusters"]["Condition"] == {"ArnEquals": {"ecs:cluster": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/staging"]}}
    assert by_sid["PassTheTaskRoles"]["Resource"] == [f"arn:aws:iam::{ACCOUNT}:role/web-task"]
    assert action.iam_statements(actions.CanaryScope(), account_id=ACCOUNT, region=REGION) == []
    ctx = actions.CanaryContext(region=REGION, account_id=ACCOUNT, service_arn=PROD, image=IMAGE)
    with pytest.raises(actions.ScopeError, match="not in this grant's scope"):
        action.check_scope(GRANT["scope"], ctx)
    assert actions.cluster_arn_of_service(STAGING) == f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/staging"
    from compass_core import actions as core_actions

    assert core_actions.captain_of("patch.canary_deploy") == "patch" and core_actions.describe_action("patch.canary_deploy")["rollback_supported"] is True


def test_a_canary_runs_the_image_on_the_twin_probes_it_and_rolls_back(monkeypatch):
    session = FakeSession()
    clock = Clock()
    probes = []

    def probe(url):
        probes.append(url)
        return 200

    result = verify.run_canary(config={"aws": {"account_id": ACCOUNT, "region": REGION}}, workspace_id="ws-a", finding_rows=[{"key": "k1"}], twin=TWIN, production_arn=PROD,
                               image=IMAGE, grant_row=GRANT, session=session, sleep=clock.sleep, clock=clock, probe=probe)
    assert result["status"] == "passed", result
    assert [s["step"] for s in result["timeline"]] == ["snapshot", "execute", "verify", "rollback"]
    assert result["staging_service"] == STAGING and session.ecs.updates[0].endswith(":8") and session.ecs.updates[-1] == TD_OLD, "up to the new revision, back to the old"
    registered = session.ecs.definitions[session.ecs.updates[0]]
    assert registered["containerDefinitions"][0]["image"] == IMAGE and registered["containerDefinitions"][1]["image"] == "nginx:1.27"
    assert set(probes) == {"http://staging-123.us-east-1.elb.amazonaws.com/", "http://staging-123.us-east-1.elb.amazonaws.com/health"}
    verified = result["verify"]
    assert verified["ok"] and verified["probe_count"] >= 4 and all(p["expected"] == 200 for p in verified["probes"]) and verified["metrics"]["available"]
    assert result["detail"].endswith("answered as before")


def test_a_failing_probe_or_worse_errors_fail_the_canary_and_still_roll_back():
    session = FakeSession()
    clock = Clock()
    answers = iter([200, 200, 200, 503])

    def probe(url):
        return next(answers, 503)

    failed = verify.run_canary(config={}, workspace_id="ws-a", finding_rows=[{"key": "k1"}], twin=TWIN, production_arn=PROD, image=IMAGE, grant_row=GRANT,
                               session=session, sleep=clock.sleep, clock=clock, probe=probe)
    assert failed["status"] == "failed" and "answered 503" in failed["detail"] and failed["timeline"][-1]["step"] == "rollback" and session.ecs.current == TD_OLD
    noisy = FakeSession(cloudwatch=FakeCloudWatch(before=0, during=40))
    worse = verify.run_canary(config={}, workspace_id="ws-a", finding_rows=[], twin=TWIN, production_arn=PROD, image=IMAGE, grant_row=GRANT,
                              session=noisy, sleep=clock.sleep, clock=clock, probe=lambda url: 200)
    assert worse["status"] == "failed" and worse["detail"].startswith("5xx went from 0 to 40")
    broken = FakeSession()
    broken.ecs.register_task_definition = lambda **kw: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
    errored = verify.run_canary(config={}, workspace_id="ws-a", finding_rows=[], twin=TWIN, production_arn=PROD, image=IMAGE, grant_row=GRANT,
                                session=broken, sleep=clock.sleep, clock=clock, probe=lambda url: 200)
    assert errored["status"] == "failed" and errored["detail"] == "RuntimeError: AccessDenied" and errored["timeline"][-1]["step"] == "rollback"


def test_the_canary_is_refused_without_an_armed_grant_and_skipped_without_a_twin():
    not_armed = dict(GRANT, grant_status="pr_open")
    result = verify.run_canary(config={}, workspace_id="ws-a", finding_rows=[], twin=TWIN, production_arn=PROD, image=IMAGE, grant_row=not_armed, session=FakeSession(),
                               sleep=lambda s: None, clock=Clock(), probe=lambda url: 200)
    assert result["status"] == "skipped" and "pr_open, not armed" in result["detail"] and result["timeline"] == []
    elsewhere = dict(GRANT, scope={"service_arns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/staging/other"]})
    assert "not in this grant's scope" in verify.run_canary(config={}, workspace_id="ws-a", finding_rows=[], twin=TWIN, production_arn=PROD, image=IMAGE, grant_row=elsewhere,
                                                           session=FakeSession(), sleep=lambda s: None, clock=Clock(), probe=lambda url: 200)["detail"]
    rows = [{"key": "k1", "workloads": [{"node_id": "td-web", "name": "web:41"}]}]
    services = {"td-web": [{"node_id": PROD, "name": "web"}]}
    assert verify.canary_for(rows, {"patch": {}}, services)["status"] == "skipped"
    assert verify.canary_for(rows, {"patch": {"canaries": {"api": TWIN}}}, services)["detail"].startswith("none of the pull request's workloads")
    configured = verify.canary_for(rows, {"patch": {"canaries": {"web": TWIN}}}, services)
    assert configured == {"status": "configured", "service": "web", "service_arn": PROD, "twin": TWIN, "finding_key": "k1"}


def test_ci_and_the_build_are_read_from_the_watchers_record_and_the_workflow_is_written_when_missing():
    record = {"github": {"head_sha": "abc1234def", "checks": {"state": "failure", "total": 3, "failed": 1, "pending": 1,
                                                              "items": [{"name": "tests", "status": "failure"}, {"name": "compass-build / build Dockerfile", "status": "pending"}, {"name": "lint", "status": "success"}]}}}
    assert verify.ci_status(record) == {"status": "failed", "detail": "failing: tests", "checks": ["tests"]}
    assert verify.build_status(record, ["Dockerfile"])["status"] == "pending" and verify.build_status(record, ["composer.json"]) == {"status": "n/a", "detail": "the fix touches no Dockerfile"}
    green = {"github": {"head_sha": "abc1234def", "checks": {"state": "success", "total": 2, "failed": 0, "pending": 0, "items": [{"name": "tests", "status": "success"}, {"name": "docker image", "status": "success"}]}}}
    assert verify.ci_status(green)["status"] == "passed" and verify.build_status(green, ["web/Dockerfile"]) == {"status": "passed", "detail": "built: docker image"}
    assert verify.build_status({"github": {"checks": {"total": 1, "items": [{"name": "tests", "status": "success"}]}}}, ["Dockerfile"])["status"] == "missing"
    assert verify.ci_status(None)["status"] == "unknown" and verify.ci_status({"github": {"checks": {"total": 0}}})["status"] == "pending"
    workflow = verify.build_workflow(["web/Dockerfile", "worker/Dockerfile.worker"])
    assert workflow.startswith("# Added by Compass") and "name: compass-build" in workflow and 'file: "web/Dockerfile", context: "web"' in workflow and "push: false" in workflow
    assert verify.repo_builds_on_pull_requests([{"name": "Build and push", "events": ["pull_request", "push"], "jobs": ["build"], "publishes": True}])
    assert verify.repo_builds_on_pull_requests([{"name": "ci", "events": ["pull_request"], "jobs": ["docker-image"], "publishes": False}])
    assert not verify.repo_builds_on_pull_requests([{"name": "Build and push", "events": ["push"], "jobs": ["build"], "publishes": True}])
    assert not verify.repo_builds_on_pull_requests([{"name": "tests", "events": ["pull_request"], "jobs": ["pytest"], "publishes": False}])


@pytest.fixture
def findings(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        store = FindingStore(table, PK)
        pr = {"url": "https://github.com/acme/web/pull/7", "number": 7, "repo": "acme/web", "branch": "beamreach/patch/20261022-abcdef", "files": ["Dockerfile"]}
        store.put({"key": "k1", "advisory_id": "CVE-2026-109618", "node_id": "f-web", "node_name": "web/Dockerfile", "repo": "acme/web", "path": "Dockerfile", "state": "pr_open",
                   "verdict": "pr", "pr": pr, "workloads": [{"node_id": "td-web", "name": "web:41", "exposed": True, "via": "image acme-web"}], "exposed": True})
        yield store


def test_verify_all_runs_the_canary_once_per_head_comments_and_flags_a_failure(findings):
    registry = MagicMock()
    registry.get.return_value = {"github": {"head_sha": "abc1234def", "checks": {"state": "success", "total": 2, "failed": 0, "pending": 0,
                                                                                   "items": [{"name": "tests", "status": "success"}, {"name": "compass-build", "status": "success"}]}}}
    comments, watches = [], []
    ecr = MagicMock()
    ecr.describe_images.return_value = {"imageDetails": [{"registryId": ACCOUNT, "imageDigest": "sha256:" + "a" * 64}]}
    read = MagicMock()
    read.client.return_value = ecr
    config = {"aws": {"account_id": ACCOUNT, "region": REGION}, "patch": {"canaries": {"web": TWIN}}}
    services = {"td-web": [{"node_id": PROD, "name": "web"}]}
    clock = Clock()
    probe_answers = {"n": 0}

    def probe(url):
        probe_answers["n"] += 1
        return 200

    report = verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[GRANT],
                               session_factory=lambda grant: FakeSession(), read_session=read, comment=lambda r, n, t: comments.append((r, n, t)),
                               set_watch=lambda r, n, s, reason: watches.append((r, n, s, reason)), now_ms=5000, sleep=clock.sleep, clock=clock, probe=probe)
    assert report["canaries"] == [{"pr": "acme/web#7", "status": "passed", "detail": report["canaries"][0]["detail"]}] and report["needs_human"] == []
    row = findings.get("k1")
    v = row["pr"]["verification"]
    assert v["ci"]["status"] == "passed" and v["build"] == {"status": "passed", "detail": "built: compass-build"} and v["canary"]["status"] == "passed" and v["ready"] is True
    assert v["canary"]["head_sha"] == "abc1234def" and v["canary"]["image"] == f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/acme-web:pr-abc1234"
    assert ecr.describe_images.call_args.kwargs == {"repositoryName": "acme-web", "imageIds": [{"imageTag": "pr-abc1234"}]}
    assert comments and comments[0][:2] == ("acme/web", 7) and comments[0][2].startswith("**Canary passed** on `web-staging`") and watches == []
    # the same head again: nothing runs twice
    again = verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[GRANT],
                              session_factory=lambda grant: FakeSession(), read_session=read, comment=lambda r, n, t: comments.append((r, n, t)), now_ms=6000, clock=clock, sleep=clock.sleep, probe=probe)
    assert again["canaries"] == [] and len(comments) == 1
    # a new head fails its canary: the pull request needs a person
    registry.get.return_value = {"github": {"head_sha": "ffff999", "checks": {"state": "success", "total": 2, "failed": 0, "pending": 0,
                                                                                "items": [{"name": "tests", "status": "success"}, {"name": "compass-build", "status": "success"}]}}}
    failed = verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[GRANT],
                               session_factory=lambda grant: FakeSession(), read_session=read, comment=lambda r, n, t: comments.append((r, n, t)),
                               set_watch=lambda r, n, s, reason: watches.append((r, n, s, reason)), now_ms=7000, clock=clock, sleep=clock.sleep, probe=lambda url: 502)
    assert failed["needs_human"] == ["acme/web#7"] and watches == [("acme/web", 7, "needs_human", "canary_failed")]
    assert findings.get("k1")["pr"]["verification"]["ready"] is False and comments[-1][2].startswith("**Canary failed**")
    # without a grant, or without the image, the canary is skipped and the pull request is still ready
    registry.get.return_value = {"github": {"head_sha": "0000001", "checks": {"state": "success", "total": 1, "failed": 0, "pending": 0, "items": [{"name": "compass-build", "status": "success"}]}}}
    skipped = verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[], read_session=read, now_ms=8000)
    assert skipped["canaries"] == [] and findings.get("k1")["pr"]["verification"]["canary"]["detail"].startswith("no grant for patch.canary_deploy")
    assert findings.get("k1")["pr"]["verification"]["ready"] is True
    ecr.describe_images.side_effect = RuntimeError("ImageNotFound")
    registry.get.return_value = {"github": {"head_sha": "0000002", "checks": {"state": "success", "total": 1, "failed": 0, "pending": 0, "items": [{"name": "compass-build", "status": "success"}]}}}
    verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[GRANT], read_session=read, now_ms=9000)
    assert findings.get("k1")["pr"]["verification"]["canary"]["detail"].startswith("no image tagged pr-0000002")
    # red CI: no canary, not ready
    registry.get.return_value = {"github": {"head_sha": "0000003", "checks": {"state": "failure", "total": 1, "failed": 1, "pending": 0, "items": [{"name": "tests", "status": "failure"}]}}}
    verify.verify_all(config=config, workspace_id="ws-a", findings=findings, registry=registry, services=services, grant_rows=[GRANT], read_session=read, now_ms=9500)
    v = findings.get("k1")["pr"]["verification"]
    assert v["ci"]["status"] == "failed" and v["ready"] is False and v["canary"]["head_sha"] == "0000002"
    assert "canary failed" not in verify.canary_comment({"status": "skipped", "detail": "x", "timeline": []}).lower()
