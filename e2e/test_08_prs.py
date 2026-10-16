"""4.9 Pull requests: the watcher (docs/e2e.md). The decoy and import pull requests opened
earlier are tracked; the fake GitHub merges, closes and reviews them; Compass, model-free,
syncs their state and parks a reviewer's request for a person."""

from __future__ import annotations

import time

from e2e import fixture

OWNER, NAME = fixture.REPO.split("/")


def _sync(stack) -> dict:
    started = stack.api.post("/api/prs/sync")
    assert started.status_code == 200, started.text
    job = stack.wait_job(started.json()["job_id"], timeout=300)
    assert job["status"] == "completed", job
    return job["result_summary"]


def _rows(stack) -> dict:
    return {r["pr_url"]: r for r in stack.api.get("/api/prs").json() if r.get("pr_url")}


def _number(url: str) -> int:
    return int(url.rsplit("/", 1)[1])


def test_prs_1_sync_follows_the_pull_requests_compass_opened_to_merged_and_closed(stack):
    _sync(stack)
    rows = _rows(stack)
    pulls = {p["number"]: p for p in stack.repo()["pulls"]}
    tracked = {url: row for url, row in rows.items() if row.get("tracked")}
    assert tracked, "the pull requests Compass opened are tracked"
    for url, row in tracked.items():
        pull = pulls[_number(url)]
        expected = "merged" if pull["merged"] else pull["state"]
        assert row["state"] == expected, (url, row["state"], expected)
    assert {"merged", "closed", "open"} <= {row["state"] for row in tracked.values()}, "the suite made one of each"


def test_prs_2_a_reviewer_s_request_is_parked_for_a_person_and_listed_under_needs_you(stack, page):
    url = stack.records["import_pr"]
    number = _number(url)
    stack.fakes.post(f"/_control/repos/{OWNER}/{NAME}/pulls/{number}/review", json={
        "body": "Please add a lifecycle rule to this bucket.", "path": f"{fixture.TERRAFORM_ROOT}/beamreach_imports.tf", "line": 6,
        "login": "alice", "state": "CHANGES_REQUESTED"}).raise_for_status()
    _sync(stack)
    deadline = time.time() + 120
    detail = {}
    while time.time() < deadline:
        detail = stack.api.get(f"/api/prs/{OWNER}/{NAME}/{number}").json()
        if detail.get("pending_revision"):
            break
        time.sleep(3)
    pending = detail.get("pending_revision") or {}
    assert pending, detail
    comments = pending["feedback"]["comments"]
    assert comments[0]["author"] == "alice" and "lifecycle rule" in comments[0]["body"]
    page.open("/workspace?tab=prs&view=needs_you")
    page.get_by_text(f"#{number}", exact=False).first.wait_for()


def test_prs_3_a_pull_request_can_be_paused_and_resumed(stack, page):
    number = _number(stack.records["import_pr"])
    paused = stack.api.post(f"/api/prs/{OWNER}/{NAME}/{number}/pause")
    assert paused.status_code == 200 and paused.json()["watch"]["status"] == "paused"
    assert _rows(stack)[stack.records["import_pr"]]["watch"]["status"] == "paused"
    resumed = stack.api.post(f"/api/prs/{OWNER}/{NAME}/{number}/resume")
    assert resumed.status_code == 200 and resumed.json()["watch"]["status"] == "watching"
    assert stack.api.post(f"/api/prs/{OWNER}/{NAME}/9999/pause").status_code == 404


def test_prs_4_the_watcher_policy_saves_and_the_next_review_is_handled_under_it(stack, page):
    before = stack.api.get("/api/prs/watcher").json()
    saved = stack.api.put("/api/prs/watcher", json={"mode": "approve", "risk_max": 4})
    assert saved.status_code == 200, saved.text
    assert {k: stack.api.get("/api/prs/watcher").json()[k] for k in ("mode", "risk_max")} == {"mode": "approve", "risk_max": 4}
    assert stack.api.put("/api/prs/watcher", json={"mode": "sometimes"}).status_code == 400
    number = _number(stack.records["import_pr"])
    stack.fakes.post(f"/_control/repos/{OWNER}/{NAME}/pulls/{number}/review", json={
        "body": "Also tag it with team=data.", "path": f"{fixture.TERRAFORM_ROOT}/beamreach_imports.tf", "line": 7, "login": "bob"}).raise_for_status()
    _sync(stack)
    deadline = time.time() + 120
    while time.time() < deadline:
        addressed = [j for j in stack.api.get("/api/jobs", params={"limit": 20}).json() if j["command"] == "address" and j["status"] == "completed"]
        if addressed and "approve" in str(addressed[0].get("result_summary")):
            break
        time.sleep(3)
    assert addressed and "approve" in str(addressed[0]["result_summary"]), addressed[:1]
    stack.api.put("/api/prs/watcher", json={"mode": before["mode"], "risk_max": before["risk_max"]}).raise_for_status()
