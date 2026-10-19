"""4.8 Code-as-cloud (docs/e2e.md): coverage, an import as a pull request, fix-drift without
a model, the pull requests listed.

Terraform runs for real in the import (it is in the image), and downloads the AWS
provider from the registry; it cannot read moto's resources, so the plan is "needs
review" (`verified: false`), which is the hermetic tier's expected result. Drift fixes
are written by the model, which this suite runs without: IAC-3 checks the refusal.
"""

from __future__ import annotations

from e2e import fixture


def _coverage(stack):
    return stack.api.get("/api/iac/coverage").json()


def test_iac_1_the_iac_tab_lists_what_terraform_does_not_manage_and_the_coverage(stack, page, mapped):
    coverage = _coverage(stack)
    unmanaged = {r["name"] for r in coverage["unmanaged_resources"]}
    drifted = {r["name"] for r in coverage["drifted_resources"]}
    assert {fixture.UNMANAGED_BUCKET} <= unmanaged and not {fixture.MANAGED_BUCKET, fixture.MANAGED_FUNCTION} & unmanaged
    assert drifted == {fixture.MANAGED_QUEUE}
    assert coverage["covered"] == 5, "the five resources in the state"
    page.open("/workspace?tab=iac")
    page.get_by_text(fixture.UNMANAGED_BUCKET, exact=False).first.wait_for()


def test_iac_2_importing_a_resource_opens_a_pull_request_with_its_import_block(stack, mapped):
    row = next(r for r in _coverage(stack)["unmanaged_resources"] if r["name"] == fixture.UNMANAGED_BUCKET)
    started = stack.api.post("/api/iac/import", json={"resource_ids": [row["resource_id"]], "repo": fixture.REPO,
                                                      "module_path": fixture.TERRAFORM_ROOT})
    assert started.status_code == 200, started.text
    job = stack.wait_job(started.json()["job_id"], timeout=900)
    assert job["status"] == "completed", job
    number = int(job["result_summary"]["pr_url"].rsplit("/", 1)[1])
    pull = next(p for p in stack.repo()["pulls"] if p["number"] == number)
    assert pull["head"].startswith("beamreach/iac-import/") and fixture.UNMANAGED_BUCKET in pull["title"]
    owner, name = fixture.REPO.split("/")
    imports = stack.fakes.get(f"/_control/repos/{owner}/{name}/file",
                              params={"path": f"{fixture.TERRAFORM_ROOT}/beamreach_imports.tf", "branch": pull["head"]}).json()["text"]
    assert "import {" in imports and f'id = "{fixture.UNMANAGED_BUCKET}"' in imports
    stack.records["import_pr"] = job["result_summary"]["pr_url"]


def test_iac_3_without_a_model_fix_drift_says_it_needs_one_and_opens_nothing(stack, mapped):
    row = next(r for r in _coverage(stack)["drifted_resources"] if r["name"] == fixture.MANAGED_QUEUE)
    before = len(stack.repo()["pulls"])
    started = stack.api.post("/api/iac/fix-drift", json={"resource_ids": [row["resource_id"]]})
    assert started.status_code == 200, started.text
    job = stack.wait_job(started.json()["job_id"], timeout=300)
    text = f"{job.get('error')} {job.get('result_summary')}"
    assert "language model" in text, job
    assert len(stack.repo()["pulls"]) == before


def test_iac_4_the_pull_requests_compass_opened_are_listed(stack, page):
    url = stack.records["import_pr"]
    listed = stack.api.get("/api/iac/prs").json()
    assert any(row.get("pr_url") == url for row in listed)
    page.open("/workspace?tab=prs")  # the IaC tab starts them; the Pull requests tab lists them
    page.get_by_text(f"#{url.rsplit('/', 1)[1]}", exact=False).first.wait_for()
