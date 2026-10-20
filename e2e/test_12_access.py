"""4.13 Access: grants by pull request (docs/e2e.md). `connect` left the two roles; allowing a
response proposes a grant and opens the access pull request on the fake GitHub; merging it in
the fake and verifying arms it (moto holds the act role's policy once "applied"); an incident
response then runs with the session recorded in the audit."""

from __future__ import annotations

import json

from e2e import fixture

OWNER, NAME = fixture.REPO.split("/")
SERVICE = f"arn:aws:ecs:{fixture.REGION}:{fixture.ACCOUNT}:service/web/web"


def _number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def test_acc_1_the_roles_are_there_and_nothing_is_granted(stack):
    access = stack.api.get("/api/access").json()
    assert access["connected"] and access["roles"]["read"].endswith("svc-inventory-reader-" + json.loads(stack.read("config/local.json"))["aws"]["install_id"])
    assert access["grants"] == [] and access["summary"]["armed"] == 0
    iam = stack.aws_client("iam")
    act = access["roles"]["act"].rsplit("/", 1)[-1]
    assert iam.list_role_policies(RoleName=act)["PolicyNames"] == [], "the act role starts empty"


def test_acc_2_allowing_a_response_opens_the_access_pull_request(stack):
    created = stack.api.post("/api/honeypots/actions", json={"action_type": "ecs_restart_task", "scope": {"service_arns": [SERVICE]}, "mode": "autopilot", "risk_max": 2})
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["grant_status"] == "pr_open" and row["armed"] is False
    job = stack.run_job("core", "access", {"verb": "pr"})
    assert job["status"] == "completed", job
    access = stack.api.get("/api/access").json()
    grant = next(g for g in access["grants"] if g["grant_id"] == row["action_id"])
    assert grant["grant_pr"], "the pull request is recorded on the grant"
    pull = stack.fakes.get(f"/_control/repos/{OWNER}/{NAME}/pulls/{_number(grant['grant_pr'])}").json()
    assert pull["state"] == "open" and "ecs:StopTask" in pull["body"]
    files = stack.fakes.get(f"/_control/repos/{OWNER}/{NAME}/file", params={"path": f"{fixture.TERRAFORM_ROOT}/services/_ops_access/main.tf", "branch": pull["head"]}).json()["text"]
    assert 'resource "aws_iam_role" "act"' in files and "ecs:StopTask" in files and "svc-inventory-reader-" in files
    stack.records["access_pr"] = grant["grant_pr"]


def test_acc_3_merging_and_applying_arms_it(stack):
    """The suite cannot run Terraform against moto: "the operator applies" is the test writing
    the act role's policy into moto the way the apply would, after the merge in the fake."""
    pr_url = stack.records["access_pr"]
    stack.fakes.post(f"/_control/repos/{OWNER}/{NAME}/pulls/{_number(pr_url)}/merge").raise_for_status()
    assert stack.run_job("prs", "sync", {})["status"] == "completed"
    module = stack.api.get("/api/access/module").json()
    act = module["act_role"]["name"]
    stack.aws_client("iam").put_role_policy(RoleName=act, PolicyName="ops", PolicyDocument=json.dumps(module["act_policy"]))
    verified = stack.api.post("/api/access/verify").json()
    grant = next(g for g in verified["grants"] if g["action_type"] == "ecs_restart_task")
    assert grant["grant_status"] == "armed" and grant["armed"], verified
    assert grant["verification"]["simulated"] is False, "moto has no SimulatePrincipalPolicy; the live checklist covers it"
    assert [h["to"] for h in grant["history"]] == ["proposed", "pr_open", "merged", "armed"]
    status = stack.api.get("/api/status").json()["access"]
    assert status["actions_armed"] == 1 and status["actions_total"] == 1


def test_acc_4_removing_disarms_now_and_opens_the_revoke_pull_request(stack):
    access = stack.api.get("/api/access").json()
    grant = next(g for g in access["grants"] if g["action_type"] == "ecs_restart_task")
    removed = stack.api.post(f"/api/access/grants/{grant['grant_id']}/remove").json()
    assert removed["grant_status"] == "disarmed" and removed["armed"] is False
    job = stack.run_job("core", "access", {"verb": "pr"})
    assert job["status"] == "completed", job
    after = next(g for g in stack.api.get("/api/access").json()["grants"] if g["grant_id"] == grant["grant_id"])
    assert after["grant_status"] == "revoke_pr_open" and after["grant_pr"]
    files = stack.fakes.get(f"/_control/repos/{OWNER}/{NAME}/file", params={"path": f"{fixture.TERRAFORM_ROOT}/services/_ops_access/main.tf",
                                                                             "branch": "beamreach/access"}).json()["text"]
    assert "ecs:StopTask" not in files, "the revoke pull request is the module without the grant"
    verify = stack.api.post("/api/access/verify").json()
    assert next(g for g in verify["grants"] if g["grant_id"] == grant["grant_id"])["grant_status"] == "revoke_pr_open", "still in IAM: not revoked yet"


def test_acc_5_the_audit_finds_a_console_change(stack):
    """A statement added in the console shows as a finding; regenerating the module is its fix."""
    access = stack.api.get("/api/access").json()
    read = access["roles"]["read"].rsplit("/", 1)[-1]
    stack.aws_client("iam").put_role_policy(RoleName=read, PolicyName="added-in-the-console",
                                            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}))
    audit = stack.api.post("/api/access/audit").json()
    kinds = {(f["kind"], f["role"]) for f in audit["findings"]}
    assert ("unexplained", "read") in kinds, audit
    assert any("s3:GetObject" in json.dumps(f) for f in audit["findings"])
    stack.aws_client("iam").delete_role_policy(RoleName=read, PolicyName="added-in-the-console")
    clean = stack.api.post("/api/access/audit").json()
    assert not [f for f in clean["findings"] if f["kind"] == "unexplained"], clean
    assert stack.api.get("/api/status").json()["access"]["audited_at"]
