"""4.16 Patches (docs/e2e.md): php2shell injected into the feed's store, the finding on the fixture
repository's Dockerfile, the fix pull request on the fake GitHub with the golden diff and the
build workflow, the Patches tab, the inbox item for a finding below every trigger, the merge
moving the finding on and the rescan marking it deployed."""

from __future__ import annotations

import time

from e2e import fixture

OWNER, NAME = fixture.REPO.split("/")
PHP2SHELL = "CVE-2026-109618"
LARAVEL = "CVE-2026-20001"

PHP2SHELL_RECORD = {
    "id": PHP2SHELL, "summary": "php2shell: memory exhaustion in the multipart parser leaves an executable file behind",
    "details": "Crafted multipart requests exhaust memory_limit; what PHP leaves behind gives pre-auth RCE.", "cvss": 9.2, "severity": "critical",
    "published": "2026-10-21T14:00:00Z", "modified": "2026-10-22T13:40:00Z", "references": ["https://calif.io/php2shell/"],
    "affected": [{"purl_type": "docker", "name": "php", "source": "cvelist", "product": "php", "ecosystem": "PHP Group/PHP",
                  "ranges": [{"introduced": "8.2.0", "fixed": "8.2.35"}, {"introduced": "8.3.0", "fixed": "8.3.36"}, {"introduced": "8.4.0", "fixed": "8.4.27"}],
                  "fixed": ["8.2.35", "8.3.36", "8.4.27"]}],
    "sources": {"cvelist": "2026-10-22T13:40:00Z"},
}
LARAVEL_RECORD = {
    "id": "GHSA-aaaa-bbbb-cccc", "aliases": [LARAVEL], "summary": "Laravel framework: a query injection in a validation rule", "modified": "2026-10-20T10:00:00Z",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:L/A:N"}],
    "affected": [{"package": {"ecosystem": "Packagist", "name": "laravel/framework"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "11.0.0"}, {"fixed": "11.2.1"}]}]}],
    "references": [{"type": "ADVISORY", "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}],
}


def _tick(stack) -> dict:
    job = stack.run_job("patch", "tick", {}, timeout=600)
    assert job["status"] == "completed", job
    return job["result_summary"]


def _findings(stack, **params) -> dict:
    return {f["advisory_id"] + "@" + f["path"]: f for f in stack.api.get("/api/patches/findings", params=params).json()}


def _file(stack, path: str, branch: str) -> str:
    r = stack.fakes.get(f"/_control/repos/{OWNER}/{NAME}/file", params={"path": path, "branch": branch})
    assert r.status_code == 200, r.text
    return r.json()["text"]


def test_pat_1_an_injected_advisory_becomes_a_finding_and_a_pull_request_with_the_golden_diff(stack, mapped, page):
    injected = stack.run_job("patch", "feed_inject", {"advisory": PHP2SHELL_RECORD})
    assert injected["status"] == "completed" and injected["result_summary"]["id"] == PHP2SHELL, injected
    report = _tick(stack)
    assert report["matched"] >= 1 and report["summary"]["open"] >= 1, report
    findings = _findings(stack)
    key = f"{PHP2SHELL}@{fixture.WEB_DOCKERFILE}"
    assert key in findings, list(findings)
    finding = findings[key]
    assert finding["package"] == "php" and finding["version"] == "8.3.30" and finding["fixed_version"] == "8.3.36" and finding["repo"] == fixture.REPO
    assert finding["verdict"] == "pr" and finding["exposed"] is False, "severe enough on its own; the fixture runs no ECS service behind a load balancer"
    assert finding["state"] == "pr_open" and finding["pr"]["url"], finding
    stack.records["patch_pr"] = finding["pr"]["url"]
    number = int(finding["pr"]["url"].rsplit("/", 1)[1])
    pull = next(p for p in stack.repo()["pulls"] if p["number"] == number)
    assert pull["title"].startswith(f"Security: {PHP2SHELL}") and pull["state"] == "open" and pull["head"].startswith("beamreach/patch/")
    assert "FROM php:8.3.30-fpm → php:8.3.36-fpm" in pull["body"] and "Revert this pull request" in pull["body"]
    assert _file(stack, fixture.WEB_DOCKERFILE, pull["head"]) == fixture.WEB_DOCKERFILE_TEXT.replace("8.3.30", "8.3.36")
    assert _file(stack, ".github/workflows/compass-build.yml", pull["head"]).startswith("# Added by Compass"), "the fixture's plan workflow builds no image"
    page.open("/workspace?tab=patches")
    page.get_by_text(PHP2SHELL, exact=False).first.wait_for()
    page.get_by_text("8.3.36", exact=False).first.wait_for()
    tracked = stack.api.get(f"/api/prs/{OWNER}/{NAME}/{number}").json()
    assert tracked["type"] == "patch_fix" and tracked["intent"]["scope"]["files"] == [".github/workflows/compass-build.yml", fixture.WEB_DOCKERFILE]


def test_pat_2_a_finding_below_every_trigger_waits_in_the_inbox_and_approving_it_opens_the_pull_request(stack, mapped):
    injected = stack.run_job("patch", "feed_inject", {"advisory": LARAVEL_RECORD})
    assert injected["result_summary"]["id"] == LARAVEL, injected
    _tick(stack)
    findings = _findings(stack)
    key = f"{LARAVEL}@{fixture.WEB_COMPOSER_LOCK}"
    assert key in findings and findings[key]["verdict"] == "inbox" and findings[key]["state"] == "open", findings.get(key)
    items = stack.api.get("/api/inbox", params={"tab": "patches", "fresh": "true"}).json()["items"]
    item = next((i for i in items if i["kind"] == "patch.approve" and i["subject"].get("repo") == fixture.REPO), None)
    assert item is not None, items
    assert item["verb"] == "approve" and LARAVEL in item["subject"]["advisories"]
    approve = next(a for a in item["actions"] if a["key"] == "approve")
    r = stack.api.post(approve["path"], json=approve["body"])
    assert r.status_code == 200 and r.json()["approved"] >= 1, r.text
    _tick(stack)
    after = _findings(stack)[key]
    assert after["state"] == "pr_open" and after["pr"]["url"] == stack.records["patch_pr"], "it lands on the repository's open fix pull request"
    number = int(after["pr"]["url"].rsplit("/", 1)[1])
    assert '"laravel/framework": "^11.2.1"' in _file(stack, fixture.WEB_COMPOSER_JSON, next(p for p in stack.repo()["pulls"] if p["number"] == number)["head"])
    assert not [i for i in stack.api.get("/api/inbox", params={"tab": "patches", "fresh": "true"}).json()["items"] if i["id"] == item["id"]]


def test_pat_3_merging_moves_the_findings_on_and_the_rescan_marks_them_deployed(stack, mapped):
    url = stack.records["patch_pr"]
    number = int(url.rsplit("/", 1)[1])
    stack.fakes.post(f"/_control/repos/{OWNER}/{NAME}/pulls/{number}/merge").raise_for_status()
    started = stack.api.post("/api/prs/sync")
    assert stack.wait_job(started.json()["job_id"], timeout=300)["status"] == "completed"
    merged = _findings(stack, state="merged")
    assert f"{PHP2SHELL}@{fixture.WEB_DOCKERFILE}" in merged, merged
    rescan = stack.api.post("/api/maps/reindex", json={"mode": "full"})
    assert stack.wait_job(rescan.json()["job_id"], timeout=900)["status"] == "completed"
    _tick(stack)
    deadline = time.time() + 60
    while time.time() < deadline:
        deployed = _findings(stack, state="deployed")
        if f"{PHP2SHELL}@{fixture.WEB_DOCKERFILE}" in deployed:
            break
        time.sleep(5)
    assert f"{PHP2SHELL}@{fixture.WEB_DOCKERFILE}" in deployed, deployed
    summary = stack.api.get("/api/patches").json()["summary"]
    assert summary["deployed"] >= 1
    status = stack.api.get("/api/status").json()
    assert status["patches"]["deployed"] >= 1
