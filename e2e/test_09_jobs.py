"""4.10 Jobs and the queue (docs/e2e.md): newest first however many there are, a failed job
and its restart, a daemon killed in the middle of a job, /api/status degrading."""

from __future__ import annotations

import json
import time

OLD_JOBS = """
import boto3, uuid
table = boto3.resource("dynamodb").Table("compass_jobs_local")
with table.batch_writer() as batch:
    for n in range(250):
        job_id = str(uuid.uuid4())
        batch.put_item(Item={"pk": "WORKSPACE#local", "sk": f"JOB#{job_id}", "job_id": job_id, "workspace_id": "local",
                             "captain": "prs", "command": "sync", "status": "completed", "queue": "compass",
                             "created_at": 1000 + n, "updated_at": 1000 + n, "events": []})
"""


def test_job_1_the_newest_jobs_are_listed_first_even_among_hundreds(stack, page):
    stack.compose("exec", "-T", "api", "python", "-c", OLD_JOBS)
    started = stack.api.post("/api/prs/sync").json()["job_id"]
    stack.wait_job(started, timeout=120)
    newest = stack.api.get("/api/jobs", params={"limit": 100}).json()
    assert newest[0]["job_id"] == started, [j["job_id"] for j in newest[:3]]
    stamps = [j["updated_at"] for j in newest]
    assert stamps == sorted(stamps, reverse=True)
    page.open("/workspace?tab=queue")
    first_row = page.locator("table tbody tr").first
    first_row.wait_for()
    assert started in first_row.inner_text()


def test_job_2_a_failed_job_shows_its_error_and_can_be_restarted(stack, page):
    failed = stack.run_job("mapper", "export", {"format": "nonsense", "out": "/compass/map/x"})
    assert failed["status"] == "failed" and failed.get("error"), failed
    page.open("/workspace?tab=queue")
    row = page.locator("tr", has=page.get_by_text(failed["job_id"], exact=True)).first
    row.wait_for()
    page.get_by_text(failed["error"][:30], exact=False).first.wait_for()
    restarted = stack.api.post(f"/api/jobs/{failed['job_id']}/restart")
    assert restarted.status_code == 200, restarted.text
    again = stack.wait_job(restarted.json()["job_id"], timeout=120)
    assert again["status"] == "failed" and again["error"] == failed["error"], "the same bad payload fails the same way"


def test_job_3_a_job_whose_daemon_died_is_marked_dead_and_new_jobs_run_after_a_restart(stack, mapped):
    started = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()["job_id"]
    stack.wait_job(started, until=("running",), timeout=120)
    stack.compose("kill", "compass")
    try:
        job = stack.wait_job(started, timeout=420)  # three minutes without a heartbeat, plus the listing that notices
        assert job["status"] == "failed" and "interrupted" in (job.get("error") or "").lower(), job
    finally:
        stack.compose("up", "-d", "compass")
    again = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()["job_id"]
    assert stack.wait_job(again, timeout=600)["status"] == "completed"


def test_job_4_status_reports_every_section_and_degrades_one_at_a_time(stack):
    status = stack.api.get("/api/status").json()
    for section in ("workspace", "app", "auth", "target", "infra_scan", "jobs", "prs", "honeypots", "update"):
        assert section in status, section
    assert status["errors"] == {}, status["errors"]
    assert status["target"]["aws_connected"] and status["target"]["github_connected"]
    path = stack.project / "config" / "local.json"
    original = path.read_text()
    config = json.loads(original)
    config["dynamodb_config"]["items"] = "compass_items_missing"  # honeypots and the PR registry read it
    config["dynamodb_config"]["audit"] = "compass_items_missing"
    try:
        path.write_text(json.dumps(config))
        broken = stack.api.get("/api/status").json()
        assert broken["errors"], "a section fails"
        assert broken["target"]["aws_connected"] and "update" in broken, "the others still answer"
    finally:
        path.write_text(original)
    time.sleep(1)
    assert stack.api.get("/api/status").json()["errors"] == {}
