"""4.11 Updates (docs/e2e.md): the banner and the Compass card against the fake release feed,
the release notes of the versions in between, the manual commands, a real update and rollback (tests/e2e/update_compose.sh), waiting for
running jobs, the policy, turning the check off, and a pinned version that differs.

The stack runs the updater service. Tests that must not let it act stop it and write its
heartbeat themselves, so the page still offers one-click updates.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Iterator

import pytest

from e2e.conftest import ROOT, VERSION


def _next(version: str, part: int = 2) -> str:
    numbers = [int(n) for n in version.split(".")[:3]]
    numbers[part] += 1
    for later in range(part + 1, 3):
        numbers[later] = 0
    return ".".join(str(n) for n in numbers)


def feed(*versions: str, min_from: str = "0.1.0") -> dict:
    releases = [{"version": v, "published_at": "2026-10-20T12:00:00Z", "notes_url": f"https://example.com/releases/v{v}",
                 "summary": f"Compass {v}: the end-to-end suite's release.", "images": {"compass": "sha256:" + "a" * 64},
                 "min_from": min_from, "requires_restart": ["api", "compass", "dashboard"], "notes": ["Reload the page after the update."]}
                for v in versions]
    stable = max(versions, key=lambda v: [int(n) for n in v.split(".")])
    return {"schema": 1, "channels": {"stable": stable}, "releases": releases}


def publish(stack, *versions: str) -> None:
    stack.fakes.put("/_control/feed", json=feed(*versions)).raise_for_status()
    stack.api.get("/api/updates", params={"refresh": True}).raise_for_status()


@pytest.fixture
def quiet_updater(stack) -> Iterator[None]:
    """The updater stopped, its heartbeat kept fresh by the test: the page offers Update, nothing is applied."""
    stack.compose("stop", "updater")
    keep = {"on": True}

    def _beat() -> None:
        while keep["on"]:
            stack.write("extensions/updater.json", json.dumps({"at": time.time(), "version": VERSION}))
            time.sleep(10)

    import threading

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    yield
    keep["on"] = False
    thread.join(timeout=15)
    for name in ("update.json", "rollback.json", "update.result.json", "update.pending"):
        stack.remove(f"extensions/{name}")
    stack.compose("start", "updater")


def test_upd_1_a_newer_release_shows_a_banner_that_can_be_dismissed_until_the_next_one(stack, page):
    newer = _next(VERSION)
    publish(stack, VERSION, newer)
    state = stack.api.get("/api/updates").json()["update"]
    assert state["available"] and state["latest"] == newer and state["release"]["notes"] == ["Reload the page after the update."]
    page.open("/workspace?tab=maps")
    banner = page.get_by_text(f"Compass {newer} is available; you run {VERSION}.", exact=False)
    banner.wait_for()
    page.get_by_role("button", name="Dismiss until the next release").click()
    page.reload()
    page.wait_for_load_state("networkidle")
    assert banner.count() == 0
    newest = _next(newer)
    publish(stack, VERSION, newer, newest)
    page.reload()
    page.get_by_text(f"Compass {newest} is available", exact=False).wait_for()
    page.open("/workspace?tab=settings&view=updates")
    page.get_by_text("Reload the page after the update.", exact=False).wait_for()


def test_upd_2_without_the_updater_the_card_shows_the_commands(stack, page):
    newer = _next(VERSION)
    publish(stack, VERSION, newer)
    stack.compose("stop", "updater")
    stack.remove("extensions/updater.json")
    try:
        assert stack.api.get("/api/updates").json()["update"]["apply"] == "manual"
        refused = stack.api.post("/api/updates/apply", json={"target": newer})
        assert refused.status_code == 409 and refused.json()["code"] == "no_updater"
        page.open("/workspace?tab=settings&view=updates")
        page.get_by_text(f"COMPASS_VERSION={newer}", exact=False).wait_for()
        page.get_by_text("docker compose pull && docker compose up -d", exact=False).wait_for()
    finally:
        stack.compose("start", "updater")


@pytest.mark.skipif(os.getenv("E2E_SKIP_COMPOSE_UPDATE") == "1", reason="E2E_SKIP_COMPOSE_UPDATE=1")
def test_upd_2_the_updater_applies_a_release_refuses_a_wrong_digest_and_rolls_back_a_broken_one():
    """A stack of its own on a local registry: from the last release to this tree with a captain
    installed, a refused digest, a release that does not start (tests/e2e/update_compose.sh)."""
    result = subprocess.run([str(ROOT / "tests" / "e2e" / "update_compose.sh")], capture_output=True, text=True, timeout=3600,
                            # E2E_KEEP is the suite's; the script cleans up after itself
                            env={**{k: v for k, v in os.environ.items() if k != "E2E_KEEP"}, "PYTHON": os.getenv("PYTHON", "python")})
    steps = "\n".join(line for line in (result.stdout + result.stderr).splitlines() if line.startswith(("e2e:", "updater:")))
    # the script prints the updater's log on a failure: keep its end with the steps
    assert result.returncode == 0 and "e2e: passed" in result.stdout, f"{steps}\n---\n{result.stderr[-4000:]}"


def test_upd_3_roll_back_from_the_card_asks_the_updater_for_the_previous_version(stack, page, quiet_updater):
    publish(stack, "0.4.0", VERSION)
    stack.write("extensions/rollback.json", json.dumps({"to": "0.4.0", "from": VERSION, "until": time.time() + 86400}))
    page.on("dialog", lambda dialog: dialog.accept())
    page.open("/workspace?tab=settings&view=updates")
    page.get_by_role("button", name="Roll back to 0.4.0").click()
    deadline = time.time() + 30
    while not stack.exists("extensions/update.json"):
        assert time.time() < deadline, "no update.json written"
        time.sleep(1)
    asked = json.loads(stack.read("extensions/update.json"))
    assert asked["target"] == "0.4.0" and asked["rollback"] is True
    page.get_by_text("Update to 0.4.0 requested", exact=False).wait_for()


def test_upd_4_an_update_waits_while_a_job_runs(stack, quiet_updater):
    """The updater's rule, against its own script: status.json says two jobs run, so the request waits."""
    stack.compose("pause", "api", "compass")  # they rewrite status.json every 30 s
    try:
        stack.write("extensions/status.json", json.dumps({"version": VERSION, "active_jobs": 2, "at": time.time(), "by": "e2e"}))
        stack.write("extensions/update.json", json.dumps({"target": _next(VERSION), "digests": {}, "requested_at": time.time(), "by": "e2e",
                                                          "mode": "manual", "force": False, "wait_minutes": 60}))
        stack.compose("start", "updater")
        deadline = time.time() + 120
        while json.loads(stack.read("extensions/update.result.json") or "{}").get("status") != "waiting":
            assert time.time() < deadline, f"the updater did not wait: {stack.read('extensions/update.result.json') or 'no result'}"
            time.sleep(3)
        assert "2 jobs are running" in json.loads(stack.read("extensions/update.result.json"))["error"]
        assert stack.exists("extensions/update.json"), "the request stays for the next try"
        stack.compose("stop", "updater")
    finally:
        stack.compose("unpause", "api", "compass")


def test_upd_5_the_policy_saves_from_the_card_and_patch_asks_for_a_patch_release_on_its_own(stack, page, quiet_updater):
    page.open("/workspace?tab=settings&view=updates")
    page.get_by_label("Updates").select_option("patch")
    deadline = time.time() + 15
    while stack.api.get("/api/updates").json()["policy"]["auto"] != "patch":
        assert time.time() < deadline, "the policy did not save"
        time.sleep(1)
    try:
        publish(stack, VERSION, _next(VERSION))
        deadline = time.time() + 120  # the daemon checks every 30 s
        while not stack.exists("extensions/update.json"):
            assert time.time() < deadline, "the daemon did not ask for the patch release"
            time.sleep(3)
        asked = json.loads(stack.read("extensions/update.json"))
        assert asked["target"] == _next(VERSION) and asked["mode"] == "auto" and asked["by"] == "policy"
        stack.remove("extensions/update.json")
        publish(stack, VERSION, _next(VERSION, part=1))  # a minor release: more than patch allows
        time.sleep(70)
        assert not stack.exists("extensions/update.json"), "patch does not apply a minor release"
    finally:
        stack.api.put("/api/updates/policy", json={"auto": "notify"}).raise_for_status()


def test_upd_6_with_the_check_off_no_request_leaves_the_stack(stack):
    stack.env(COMPASS_UPDATE_CHECK="off")
    stack.restart("api", "compass")
    try:
        before = len(stack.fakes.get("/_control/feed/requests").json())
        notes_before = len(stack.fakes.get("/_control/notes/requests").json())
        state = stack.api.get("/api/updates", params={"refresh": True}).json()["update"]
        assert state["apply"] == "off" and state["latest"] is None
        stack.api.get("/api/settings/bootstrap").raise_for_status()
        time.sleep(40)  # a daemon tick
        assert len(stack.fakes.get("/_control/feed/requests").json()) == before
        assert len(stack.fakes.get("/_control/notes/requests").json()) == notes_before, "no release notes either"
    finally:
        stack.env(COMPASS_UPDATE_CHECK=None)
        stack.restart("api", "compass")
    requests = stack.fakes.get("/_control/feed/requests").json()
    stack.api.get("/api/updates", params={"refresh": True})
    seen = stack.fakes.get("/_control/feed/requests").json()
    assert len(seen) > len(requests) and seen[-1]["user_agent"] == f"compass-update/{VERSION}", "on again: one anonymous request"


def test_upd_7_the_page_says_when_the_compose_file_asks_for_another_version(stack, page):
    stack.env(COMPASS_VERSION="0.0.1")
    stack.restart("api")
    try:
        page.open("/workspace?tab=queue")
        page.get_by_text(f"The compose file asks for 0.0.1 but {VERSION} is running", exact=False).wait_for()
    finally:
        stack.env(COMPASS_VERSION=VERSION)
        stack.restart("api")


def notes_feed(*versions: str) -> str:
    items = "".join(
        f"<item><title>Compass {v} — Release {v}</title><link>https://beamreach.ai/compass/releases/{v}.html</link>"
        f"<pubDate>Tue, 20 Oct 2026 12:00:00 +0000</pubDate><description>What {v} changes, in a paragraph.</description>"
        f"<content:encoded><![CDATA[<p>HTML for feed readers</p>]]></content:encoded>"
        f"<compass:version>{v}</compass:version><compass:headline>Release {v}</compass:headline>"
        f"<compass:feature><compass:title>Feature of {v}</compass:title><compass:body>Body of {v}.</compass:body></compass:feature>"
        f"<compass:after>After {v}, reload the page.</compass:after></item>"
        for v in versions
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
            f' xmlns:compass="https://beamreach.ai/compass/releases/ns/1"><channel><title>Compass releases</title>{items}</channel></rss>')


def test_upd_8_the_card_shows_the_release_notes_of_every_version_in_between(stack, page):
    newer, newest = _next(VERSION), _next(_next(VERSION))
    stack.fakes.put("/_control/notes", content=notes_feed(VERSION, newer, newest)).raise_for_status()
    try:
        publish(stack, VERSION, newer, newest)
        state = stack.api.get("/api/updates").json()["update"]
        assert [n["version"] for n in state["release_notes"]] == [newest, newer], "after the running version, newest first"
        assert stack.fakes.get("/_control/notes/requests").json()[-1]["user_agent"] == f"compass-update/{VERSION}"
        page.open("/workspace?tab=maps")
        page.get_by_role("link", name="What's new").click()
        page.get_by_text(f"Release {newest}", exact=False).first.wait_for()
        page.get_by_text(f"After {newest}, reload the page.", exact=False).wait_for()
        page.get_by_text(f"Release {newer}", exact=False).first.click()
        page.get_by_text(f"Body of {newer}.", exact=False).wait_for()
        assert page.get_by_role("link", name="Read on beamreach.ai").count() >= 1
        assert page.get_by_text("HTML for feed readers").count() == 0
    finally:
        stack.fakes.put("/_control/notes", content="").raise_for_status()
