"""4.14 Demo mode (docs/e2e.md): the stack with APP_MODE=demo, the synthetic world seeded
through the CLI beside the scanned account, a rescan that leaves it alone."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from e2e import fixture


@pytest.fixture(scope="module")
def demo_mode(stack):
    """APP_MODE=demo on every service for this file, production again afterwards."""
    stack.env(APP_MODE="demo")
    stack.restart("api", "compass", "dashboard")
    try:
        yield
    finally:
        stack.env(APP_MODE=None)
        stack.restart("api", "compass", "dashboard")


def _graph(stack) -> Dict[str, Any]:
    return stack.api.get("/api/maps", params={"limit": 10000}).json()


def _synthetic(graph: Dict[str, Any]):
    return [n for n in graph["nodes"] if (n.get("attributes") or {}).get("synthetic") is True]


def test_demo_1_the_world_is_seeded_beside_the_account_and_a_rescan_leaves_it_alone(stack, mapped, demo_mode):
    before = _graph(stack)
    assert not _synthetic(before), "the scanned account has no synthetic nodes"
    real = {n["resource_id"] for n in before["nodes"]}

    seeded = stack.cli("demo", "seed", "resources=1000", "seed=7")
    assert seeded["scope"] == "demo:seed:7" and seeded["scan"]["status"] == "completed", seeded
    assert seeded["scan"]["added"] == seeded["nodes"] + seeded["edges"] and seeded["scan"]["removed"] == 0
    synthetic_rows = {(r["source"], r["scope"]): r for r in seeded["map"] if r["synthetic"]}
    assert set(synthetic_rows) == {("aws_scan", "demo:seed:7"), ("git_scan", "demo:seed:7"), ("tf_state", "demo:seed:7")}, seeded["map"]
    assert any(r["source"] == "aws_scan" and r["scope"] == "aws:full" and not r["synthetic"] for r in seeded["map"]), "the real scan's rows are listed too"

    after = _graph(stack)
    synthetic = _synthetic(after)
    assert len(synthetic) == sum(1 for n in after["nodes"] if n["resource_id"] not in real), "every new node is synthetic"
    assert len(synthetic) > 500 and all(n.get("account_id") == fixture.ACCOUNT for n in synthetic if n.get("resource_type") not in ("github_repo", "tf_root_module", "tf_state_file", "tf_state_instance"))
    assert real <= {n["resource_id"] for n in after["nodes"]}, "the scanned account is still there"
    coverage = stack.api.get("/api/iac/coverage").json() if stack.api.get("/api/iac/coverage").status_code == 200 else None
    if coverage is not None:
        assert coverage.get("total", 0) > 500, coverage

    # a real rescan: the account's rows are rewritten, the world is untouched
    started = stack.api.post("/api/maps/reindex", json={"mode": "full"}).json()
    job = stack.wait_job(started["job_id"], timeout=900)
    assert job["status"] == "completed", job
    rescanned = _graph(stack)
    assert {n["resource_id"] for n in _synthetic(rescanned)} == {n["resource_id"] for n in synthetic}
    assert real <= {n["resource_id"] for n in rescanned["nodes"]}

    # the same seed again changes nothing
    again = stack.cli("demo", "seed", "resources=1000", "seed=7")
    assert (again["scan"]["added"], again["scan"]["changed"], again["scan"]["removed"]) == (0, 0, 0), again["scan"]


def _within(inner: Dict[str, float], outer: Dict[str, float]) -> bool:
    return inner["x"] >= outer["x"] - 1 and inner["y"] >= outer["y"] - 1 and \
        inner["x"] + inner["width"] <= outer["x"] + outer["width"] + 1 and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + 1


def test_demo_2_the_demo_is_marked_on_every_screen_and_in_the_exported_map(stack, page, phone_page, mapped, demo_mode):
    shots = Path("e2e/.artifacts/demo")
    shots.mkdir(parents=True, exist_ok=True)

    page.open("/workspace?tab=maps", wait_for="Target account")
    page.locator("aside [data-demo-badge]").first.wait_for()
    page.wait_for_function("document.title.includes('demo')")  # set once the shell knows the mode
    banner = page.locator("[data-demo-banner]")
    banner.wait_for()
    assert "resets on Sunday" in banner.inner_text()
    watermark = page.locator("[data-map-watermark]")
    watermark.wait_for()
    card = watermark.locator("xpath=../..")  # the map card the canvas sits in
    box, card_box = watermark.bounding_box(), card.bounding_box()
    assert box and card_box and _within(box, card_box), (box, card_box)
    assert box["x"] + box["width"] > card_box["x"] + card_box["width"] * 0.7 and box["y"] > card_box["y"] + card_box["height"] * 0.7, "lower right"
    watermark.scroll_into_view_if_needed()
    page.screenshot(path=str(shots / "maps-desktop.png"), full_page=False)
    run_it = page.locator("aside").get_by_role("link", name="Run it yourself")
    assert run_it.get_attribute("href") == "https://beamreach.ai/compass/install.html"
    page.get_by_role("button", name="Dismiss the demo notice").click()
    assert banner.count() == 0
    page.open("/workspace?tab=honeypots", wait_for="Target account")
    assert banner.count() == 0, "dismissed for the session"
    page.locator("aside [data-demo-badge]").first.wait_for()

    phone_page.open("/workspace?tab=maps")
    phone_page.get_by_role("button", name="Open menu").wait_for()
    phone_page.locator("[data-demo-badge]").first.wait_for()  # the top bar's chip, the drawer closed
    phone_page.locator("[data-map-watermark]").wait_for()
    phone_page.screenshot(path=str(shots / "maps-phone.png"), full_page=False)
    phone_page.locator("[data-map-watermark]").scroll_into_view_if_needed()
    phone_page.screenshot(path=str(shots / "maps-phone-canvas.png"), full_page=False)

    exported = stack.cli("mapper", "export", "format=html", "out=/compass/map/demo-export.html")
    assert exported["nodes"] > 0
    html = stack.read("map/demo-export.html") or ""
    assert 'id="wm"' in html and "Synthetic resource (demo world)" in html


def _write_demo_block(stack, **block: Any) -> None:
    """The `demo` block in the install's config file (config/local.json), as the operator would set it."""
    config = json.loads(stack.read("config/local.json") or "{}")
    config["demo"] = {**(config.get("demo") or {}), **block}
    stack.write("config/local.json", json.dumps(config, indent=2) + "\n")


def _visitor(stack, user_id: str) -> Dict[str, str]:
    return {"X-User-Id": user_id}


def test_demo_3_a_visitor_trips_a_pre_armed_decoy_and_it_re_arms_after_the_response(stack, page, mapped, demo_mode):
    """Trip it through the fake alert path: the trip job's STS call hits moto, the test puts the
    alert on the queue as CloudTrail would, the autopilot deactivates the key, the demo-rearm
    service rolls it back and arms the decoy again; a second visitor meanwhile lands on the incident."""
    from e2e.test_06_decoys import _action, apply_aws_credential, ensure_trail, incidents, plant

    # a pre-armed decoy with its secret stored, an autopilot response, the decoy trippable; the re-arm ticks fast
    planted = plant(stack, "aws_credential", "e2e-demo-trip")
    key = apply_aws_credential(stack, planted)
    ensure_trail(stack)
    owner, repo = fixture.REPO.split("/")
    stack.fakes.post(f"/_control/repos/{owner}/{repo}/pulls/{planted['pull']['number']}/merge").raise_for_status()
    armed = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/arm", json={"secret_access_key": planted["secret"]})
    assert armed.status_code == 200, armed.text
    _action(stack, "autopilot")
    _write_demo_block(stack, trippable=[planted["id"]], cooldown_seconds=300, trips_per_visitor_per_hour=3)
    stack.env(COMPASS_DEMO_REARM="5")
    stack.restart("api", "compass")
    try:
        assert stack.api.post(f"/api/honeypots/tokens/{planted['id']}/test-fire").status_code == 404, "test-fire is not part of the demo"
        before = stack.api.get("/api/demo").json()
        assert planted["id"] in before["trippable"] and before["cooldowns"] == []

        tripped = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/trip", headers=_visitor(stack, "visitor-a"))
        assert tripped.status_code == 200, tripped.text
        first = tripped.json()
        assert first["state"] == "tripped" and first["by"] == "you" and first["job_id"] and 290 <= first["seconds_left"] <= 300
        stack.records["demo_decoy"] = {**planted, "key": key}

        # the second visitor, in the cooldown, is told and gets no second fire
        again = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/trip", headers=_visitor(stack, "visitor-b")).json()
        assert again["state"] == "cooldown" and again["by"] == "a visitor" and again["job_id"] == first["job_id"]

        # the job used the key (one STS call against moto) and now waits for CloudTrail; the test is CloudTrail
        deadline = time.time() + 120
        while not (stack.api.get("/api/demo").json()["cooldowns"] or [{}])[0].get("fired_at"):
            assert time.time() < deadline, "the trip job did not fire"
            time.sleep(2)
        message = {"token_id": planted["id"], "workspace_id": "local", "event_id": f"e2e-trip-{time.time_ns()}", "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "event_name": "GetCallerIdentity", "region": fixture.REGION, "access_key_id": key,
                   "principal_arn": f"arn:aws:iam::{fixture.ACCOUNT}:user/service/{planted['user']}", "source_ip": "203.0.113.42",
                   "user_agent": "Boto3/1.39 compass-demo-trip", "error_code": "", "vpc_endpoint_id": "", "service": "sts"}
        stack.aws_client("sqs").send_message(QueueUrl=f"{stack.moto_url}/{fixture.ACCOUNT}/compass-decoy-alerts-local", MessageBody=json.dumps(message))

        job = stack.wait_job(first["job_id"], timeout=180)
        assert job["status"] == "completed", job
        result = job["result_summary"]
        assert result["status"] == "delivered" and result["incident_id"], result
        incident_id = result["incident_id"]
        assert any(e["message"].startswith("Using the decoy's key") for e in job["events"]) and any("alert arrived" in e["message"] for e in job["events"])
        assert incident_id in {i["incident_id"] for i in incidents(stack)}
        detail = stack.api.get(f"/api/honeypots/incidents/{incident_id}").json()
        assert not (detail.get("incident") or detail).get("is_test"), "a trip is a real incident"
        late = stack.api.post(f"/api/honeypots/tokens/{planted['id']}/trip", headers=_visitor(stack, "visitor-c")).json()
        assert late["state"] == "cooldown" and late["incident_id"] == incident_id, "a late visitor lands on the open incident"

        # the response runs on its own and verifies; then the key is rolled back and the decoy armed again
        iam = stack.aws_client("iam")
        deadline = time.time() + 240
        while True:
            row = (stack.api.get("/api/demo").json()["cooldowns"] or [{}])[0]
            if row.get("rearmed_at"):
                break
            assert time.time() < deadline, f"not re-armed: {row}"
            time.sleep(3)
        assert iam.list_access_keys(UserName=planted["user"])["AccessKeyMetadata"][0]["Status"] == "Active"
        token = stack.api.get(f"/api/honeypots/tokens/{planted['id']}").json()
        assert token["status"] == "armed"
        verified = stack.api.get(f"/api/honeypots/incidents/{incident_id}").json()
        inc = verified.get("incident") or verified
        assert inc.get("action_verified_at") and inc.get("status") != "closed", "the incident stays for other visitors to read"

        # on the page: the button shows the trip in force, and the link opens the incident
        page.open("/workspace?tab=honeypots&view=honeytokens", wait_for="e2e-demo-trip")
        page.get_by_text("e2e-demo-trip", exact=True).first.click()
        button = page.locator("[data-trip-it]")
        button.wait_for()
        assert "Tripped" in button.inner_text() and button.is_disabled()
        assert page.get_by_role("button", name="Test fire").count() == 0
        page.locator("[data-trip-state]").get_by_role("link", name="open it").click()
        page.wait_for_url(f"**incident={incident_id}**")
        page.get_by_role("button", name="Close", exact=True).wait_for()  # the incident drawer
    finally:
        _action(stack, "off")
        stack.env(COMPASS_DEMO_REARM=None)
        stack.restart("api", "compass")


def _tour(stack) -> Dict[str, Any]:
    return stack.api.get("/api/demo/tour").json()


def _wait_step(stack, step: str, *, timeout: float = 60) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        tour = _tour(stack)
        done = {s["id"]: s for s in tour["steps"]}[step]
        if done["done"]:
            return tour
        assert time.time() < deadline, f"step {step} not done: {done}"
        time.sleep(2)


def test_demo_4_the_tour_walks_from_the_map_to_the_install_page(stack, page, phone_page, mapped, demo_mode):
    """Builds on DEMO-3: the same pre-armed decoy, tripped again by this visitor (the page's session),
    the alert sent by the test as CloudTrail would, the response and the re-arm; the client-only
    steps complete from the page, one step is skipped, the last opens the install page."""
    from e2e.test_06_decoys import _action

    decoy = stack.records.get("demo_decoy")
    assert decoy, "DEMO-3 planted and armed the decoy this tour trips"
    _write_demo_block(stack, trippable=[decoy["id"]], cooldown_seconds=900, trips_per_visitor_per_hour=3)
    _action(stack, "autopilot")
    stack.env(COMPASS_DEMO_REARM="5")
    stack.restart("api", "compass")
    try:
        # the open edition has one visitor: DEMO-3's page already met the decoys and saw the trip; the rest is this test's
        tour = _tour(stack)
        done = {s["id"]: s["done"] for s in tour["steps"]}
        assert list(done) == ["map", "unmanaged", "import_pr", "decoys", "trip", "response", "run_it"] and not done["map"] and not done["unmanaged"]

        # the world (a no-op when DEMO-1 seeded it): "payments" is a synthetic team's name
        stack.cli("demo", "seed", "resources=1000", "seed=7")
        # the panel opens by itself on a first visit (else from the pill); the map's search selects a node: step 1 done
        page.open("/workspace?tab=maps&q=payments", wait_for="Node details")
        pill = page.locator("[data-tour-pill]")
        panel = page.locator("[data-tour-panel]")
        page.locator("[data-tour-panel], [data-tour-pill]").first.wait_for()
        if pill.count():
            pill.click()
        panel.wait_for()
        assert "Tour · " in panel.inner_text()
        _wait_step(stack, "map")
        page.locator("[data-tour-step=unmanaged]").get_by_role("link", name="Open →").click()
        page.wait_for_url("**tab=iac**")
        page.get_by_text(fixture.UNMANAGED_BUCKET, exact=False).first.wait_for()
        _wait_step(stack, "unmanaged")

        # "Skip" on the import step (IAC-2 proves the import itself; the API test proves the check)
        page.locator("[data-tour-step=import_pr]").get_by_role("button", name="Skip").click()
        deadline = time.time() + 20
        while not {s["id"]: s for s in _tour(stack)["steps"]}["import_pr"]["skipped"]:
            assert time.time() < deadline, _tour(stack)
            time.sleep(1)
        page.locator("[data-tour-step=import_pr]").get_by_text("skipped", exact=True).wait_for(timeout=15_000)  # the next poll at the latest

        # the decoys: the step's link (the same URL the panel offers while the step is current) expands the
        # trippable decoy's detail (step 4), where "Trip it" waits for the previous cooldown to lapse
        page.open(f"/workspace?tab=honeypots&view=honeytokens&token={decoy['id']}")
        button = page.locator("[data-trip-it]")
        button.wait_for()
        _wait_step(stack, "decoys")
        deadline = time.time() + 330  # DEMO-3's cooldown is five minutes
        while button.is_disabled():
            assert time.time() < deadline, f"Trip it still disabled: {button.inner_text()}"
            time.sleep(3)
        button.click()
        page.locator("[data-trip-state]").wait_for()
        deadline = time.time() + 120
        while not next((r for r in stack.api.get("/api/demo").json()["cooldowns"] if r["honeytoken_id"] == decoy["id"]), {}).get("fired_at"):
            assert time.time() < deadline, "the trip job did not fire"
            time.sleep(2)
        message = {"token_id": decoy["id"], "workspace_id": "local", "event_id": f"e2e-tour-{time.time_ns()}", "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "event_name": "GetCallerIdentity", "region": fixture.REGION, "access_key_id": decoy["key"],
                   "principal_arn": f"arn:aws:iam::{fixture.ACCOUNT}:user/service/{decoy['user']}", "source_ip": "203.0.113.43",
                   "user_agent": "Boto3/1.39 compass-demo-trip", "error_code": "", "vpc_endpoint_id": "", "service": "sts"}
        stack.aws_client("sqs").send_message(QueueUrl=f"{stack.moto_url}/{fixture.ACCOUNT}/compass-decoy-alerts-local", MessageBody=json.dumps(message))
        # the step was done already (DEMO-3 showed this visitor a trip in force); now it is their own trip
        deadline = time.time() + 180
        while True:
            trip_step = {s["id"]: s for s in _tour(stack)["steps"]}["trip"]
            if trip_step["done"] and trip_step["detail"].get("by") == "you" and trip_step["detail"].get("incident_id"):
                break
            assert time.time() < deadline, trip_step
            time.sleep(3)
        incident_id = trip_step["detail"]["incident_id"]

        # the response verifies on its own: step 6; the step's link is the incident
        tour = _wait_step(stack, "response", timeout=240)
        assert {s["id"]: s for s in tour["steps"]}["response"]["detail"]["incident_id"] == incident_id
        page.open(f"/workspace?tab=honeypots&view=history&incident={incident_id}")
        page.get_by_role("button", name="Close", exact=True).wait_for()
        page.keyboard.press("Escape")

        # the last step: the install page (a new tab); then the tour is done and the pill says so
        page.open("/workspace?tab=maps", wait_for="Target account")
        page.locator("[data-tour-panel], [data-tour-pill]").first.wait_for()
        if pill.count():
            pill.click()
        page.locator("[data-tour-step=run_it]").get_by_role("link", name="Install Compass", exact=False).wait_for()
        with page.context.expect_page() as popup:
            page.locator("[data-tour-step=run_it]").get_by_role("link", name="Install Compass", exact=False).click()
        popup.value.close()
        tour = _wait_step(stack, "run_it")
        assert tour["finished"] is True and tour["completed"] == 6
        panel.get_by_text("6 of 7", exact=False).wait_for(timeout=15_000)  # the panel's next poll
        page.get_by_role("button", name="Collapse the tour").click()
        pill.wait_for()
        assert "6 of 7" in pill.inner_text()
        page.open("/workspace?tab=maps", wait_for="Target account")
        pill.wait_for()

        # on a phone the pill does not cover the menu button
        phone_page.open("/workspace?tab=maps")
        menu = phone_page.get_by_role("button", name="Open menu")
        menu.wait_for()
        widget = phone_page.locator("[data-tour-panel], [data-tour-pill]").first
        widget.wait_for()
        a, b = menu.bounding_box(), widget.bounding_box()
        assert a and b and (a["x"] + a["width"] <= b["x"] or a["y"] + a["height"] <= b["y"]), (a, b)
    finally:
        _action(stack, "off")
        _write_demo_block(stack, trippable=[])
        stack.env(COMPASS_DEMO_REARM=None)
        stack.restart("api", "compass")


def test_demo_5_the_reset_brings_the_golden_world_back_and_status_reads_it(stack, page, mapped, demo_mode):
    """Builds on DEMO-3 and DEMO-4: their incidents, trips and tour are what the reset clears. The
    visitor's pull request is the decoy DEMO-3 planted; the seeded pull request is new."""
    from e2e.test_06_decoys import incidents

    decoy = stack.records.get("demo_decoy")
    assert decoy, "DEMO-3 planted the decoy whose pull request the reset closes"
    # the block names the world DEMO-1 and DEMO-4 seeded, so the reset's seed is a no-op scan
    _write_demo_block(stack, trippable=[decoy["id"]], visitor_repo=fixture.REPO, seeded_pr={"reviewer": "demo-reviewer"}, world={"resources": 1000, "seed": 7})
    stack.restart("api", "compass")
    try:
        assert any(not i.get("closed_at") for i in incidents(stack)), "DEMO-3 and DEMO-4 left open incidents"
        owner, name = fixture.REPO.split("/")

        result = stack.cli("demo", "reset", timeout=900)
        assert result["decoys"]["incidents_closed"] >= 1 and result["decoys"]["rearmed"] == [decoy["id"]], result["decoys"]
        assert result["pull_requests"]["closed"] >= 1 and result["pull_requests"]["failed"] == [], result["pull_requests"]
        assert result["seeded_pr"]["number"] and result["world"]["unchanged"] is True and result["rescan_job_id"], result
        assert result["rows_cleared"] >= 2 and "Demo reset:" in result["summary"]

        assert all(i.get("closed_at") for i in incidents(stack)), "every incident closed"
        closed = [i for i in incidents(stack) if i.get("resolution") == "demo reset"]
        assert closed, "closed by the reset, not by a person"
        after = {p["number"]: p for p in stack.repo()["pulls"]}
        planted = after[decoy["pull"]["number"]]
        assert planted["state"] in ("closed", "merged"), planted["state"]
        gone = stack.fakes.get(f"/github/repos/{owner}/{name}/git/ref/heads/{planted['head']}", headers={"Authorization": "token x"})
        assert gone.status_code == 404, "the visitor's branch is deleted"
        seeded = after[result["seeded_pr"]["number"]]
        labels = [label["name"] if isinstance(label, dict) else label for label in seeded.get("labels", [])]
        assert seeded["state"] == "open" and "demo" in labels and seeded["head"] == "beamreach/demo/payments-variables", seeded
        reviews = stack.fakes.get(f"/github/repos/{owner}/{name}/pulls/{seeded['number']}/reviews", headers={"Authorization": "token x"}).json()
        assert reviews and reviews[0]["state"] == "CHANGES_REQUESTED"
        assert stack.api.get("/api/demo").json()["cooldowns"] == [] and stack.api.get("/api/demo/tour").json()["completed"] == 0
        assert stack.api.get(f"/api/honeypots/tokens/{decoy['id']}").json()["status"] == "armed"
        job = stack.wait_job(result["rescan_job_id"], timeout=900)
        assert job["status"] == "completed", job
        assert len(_synthetic(_graph(stack))) > 500, "the world is still there after the rescan"

        # the PR watcher syncs the seeded pull request: the reviewer's request lands under Needs you
        stack.api.post("/api/prs/sync").raise_for_status()
        deadline = time.time() + 120
        while True:
            record = stack.api.get(f"/api/prs/{owner}/{name}/{seeded['number']}").json()
            if record.get("pending_revision") or (record.get("watch") or {}).get("status") == "needs_human":
                break
            assert time.time() < deadline, record
            time.sleep(3)
        page.open("/workspace?tab=prs&view=needs_you")
        page.get_by_text(f"#{seeded['number']}", exact=False).first.wait_for()

        shown = stack.cli("demo", "status")
        assert shown["last_reset"]["reset_at"] == result["started_at"] and shown["next_reset"].endswith("03:00:00+00:00")
        assert shown["cooldowns_in_force"] == [] and shown["tour_completions_this_week"] == 0
        assert all(p["number"] != seeded["number"] for p in shown["visitor_prs_open"]), "the seeded pull request is not a visitor's"
    finally:
        _write_demo_block(stack, trippable=[], visitor_repo="", seeded_pr={})
        stack.restart("api", "compass")


def test_demo_1b_outside_demo_mode_the_seed_refuses_in_one_line(stack):
    """Last in the file: it leaves demo mode off, as the module fixture does."""
    stack.env(APP_MODE=None)
    stack.restart("api", "compass")
    err = stack.cli("demo", "seed", "resources=100", expect=2)
    assert "runs only in demo mode" in err and "Traceback" not in err
