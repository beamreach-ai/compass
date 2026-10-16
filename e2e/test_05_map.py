"""4.6 The map (docs/e2e.md): a scan of the seeded account, its progress, coverage and
drift, the map's controls, a re-scan, the exports, a failed scan."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from e2e import fixture


def _graph(stack) -> Dict[str, Any]:
    return stack.api.get("/api/maps", params={"limit": 5000}).json()


def node(graph: Dict[str, Any], name: str, type_hint: str = "") -> Optional[Dict[str, Any]]:
    """The AWS node for a seeded resource, by its name in the id (an ARN or a URL)."""
    for candidate in graph["nodes"]:
        rid = str(candidate.get("resource_id") or "")
        if candidate.get("source") == "aws_scan" and (rid.endswith(name) or rid.endswith(f":{name}") or f"/{name}" in rid) and type_hint in str(candidate.get("resource_type")):
            return candidate
    return None


def attrs(item: Dict[str, Any]) -> Dict[str, Any]:
    return item.get("attributes") or {}


def test_map_1_a_scan_started_from_the_page_shows_its_progress_here_and_on_other_tabs(stack, page, mapped):
    page.open("/workspace?tab=maps", wait_for="Run mapping")
    page.get_by_role("button", name="Run mapping").click()
    panel_bar = page.get_by_role("progressbar", name="Mapping progress")
    panel_bar.wait_for(timeout=60_000)
    page.get_by_text("Re-scan in progress", exact=False).wait_for()
    page.get_by_text("elapsed", exact=False).first.wait_for()
    page.open("/workspace?tab=queue")
    banner = page.get_by_text("Mapping your account", exact=False).first
    if page.get_by_role("progressbar", name="Mapping progress").count():  # the scan may already be done on a fast machine
        banner.wait_for()
    jobs = [j for j in stack.api.get("/api/jobs", params={"limit": 20}).json() if j["captain"] == "mapper"]
    latest = stack.wait_job(jobs[0]["job_id"], timeout=900)
    assert latest["status"] == "completed", latest
    regions = [e for e in latest["events"] if e["message"].startswith("Scanning region")]
    assert regions and all({"region", "index", "total", "resources"} <= set(e.get("data") or {}) for e in regions), "the progress data the bar reads"


def test_map_2_the_map_has_the_seeded_resources_with_coverage_and_drift(stack, mapped):
    graph = _graph(stack)
    managed = {name: node(graph, name) for name in (fixture.MANAGED_BUCKET, fixture.MANAGED_QUEUE, fixture.MANAGED_FUNCTION, fixture.MANAGED_ROLE)}
    unmanaged = {name: node(graph, name) for name in (fixture.UNMANAGED_BUCKET, fixture.UNMANAGED_QUEUE)}
    missing = [n for n, found in {**managed, **unmanaged}.items() if found is None]
    assert not missing, f"not on the map: {missing}"
    assert all(attrs(found).get("coverage_score") == 1 for found in managed.values()), {n: attrs(f).get("coverage_score") for n, f in managed.items()}
    assert all(attrs(found).get("coverage_score") == 0 for found in unmanaged.values())
    queue = attrs(managed[fixture.MANAGED_QUEUE])
    assert queue.get("drift_status") == "drifted"
    assert any(f.get("field") == "visibilityTimeout" and str(f.get("actual")) == str(fixture.LIVE_VISIBILITY)
               for f in queue.get("drift_fields") or []), queue.get("drift_fields")
    types = {n["resource_type"] for n in graph["nodes"]}
    assert {"github_repo", "tf_root_module", "tf_state_file"} <= types, types


def test_map_3_search_focus_resource_types_and_overlays_work(page, mapped):
    page.open("/workspace?tab=maps", wait_for="Run mapping")
    page.get_by_text("RESOURCE TYPES", exact=False).wait_for()
    page.get_by_placeholder("Search node name or ARN").fill(fixture.MANAGED_QUEUE)
    page.get_by_role("button", name="Focus", exact=True).click()
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Clear", exact=True).click()
    page.get_by_role("button", name="None", exact=True).click()
    assert page.locator("input[type=checkbox]:checked").count() <= 1  # "Focus on selection" may stay
    page.get_by_role("button", name="All", exact=True).click()
    for overlay in ("Coverage", "Traffic", "Activity"):
        button = page.get_by_role("button", name=overlay, exact=True)
        button.click()
        assert "bg-gray-900" in (button.get_attribute("class") or "")
    assert not [e for e in page.console_errors if "favicon" not in e], page.console_errors


def test_map_4_a_resource_deleted_from_the_account_leaves_the_map_on_the_next_scan(stack, mapped):
    stack.aws_client("sqs").delete_queue(QueueUrl=fixture.queue_url(fixture.UNMANAGED_QUEUE, stack.moto_url))
    started = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()
    job = stack.wait_job(started["job_id"], timeout=900)
    assert job["status"] == "completed", job
    assert node(_graph(stack), fixture.UNMANAGED_QUEUE) is None
    assert node(_graph(stack), fixture.MANAGED_QUEUE) is not None


def test_map_5_the_map_exports_as_json_dot_and_html_and_the_viewer_serves_it(stack, page, mapped):
    for fmt, name in (("json", "map.json"), ("dot", "map.dot"), ("html", "index.html")):
        out = stack.cli("mapper", "export", f"format={fmt}", f"out=/compass/map/{name}")
        assert out["nodes"] > 0 and out["edges"] > 0, out
    exported = json.loads(stack.read("map/map.json"))
    assert len(exported["nodes"]) > 0
    assert stack.read("map/map.dot").startswith(("digraph", "strict digraph"))
    page.open("/map/")
    assert fixture.MANAGED_FUNCTION in page.content()


def test_map_7_a_scan_that_fails_says_why_and_a_retry_works(stack, page, mapped):
    # moto keeps the account in memory, so it is taken off the network rather than stopped:
    # the daemon cannot reach AWS, and still reaches its own DynamoDB to record the failure
    network, container = f"{stack.project.name}_default", f"{stack.project.name}-moto-1"
    import subprocess
    import time

    subprocess.run(["docker", "network", "disconnect", network, container], check=True)
    try:
        started = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()
        job = stack.wait_job(started["job_id"], timeout=900)
        assert job["status"] == "failed" and job.get("error"), job
        page.open("/workspace?tab=queue")
        page.get_by_text(job["error"][:40], exact=False).first.wait_for()
    finally:
        subprocess.run(["docker", "network", "connect", "--alias", "moto", network, container], check=True)
    time.sleep(2)
    retry = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()
    assert stack.wait_job(retry["job_id"], timeout=900)["status"] == "completed"
