"""4.14 Demo mode (docs/e2e.md): the stack with APP_MODE=demo, the synthetic world seeded
through the CLI beside the scanned account, a rescan that leaves it alone."""

from __future__ import annotations

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


def test_demo_1b_outside_demo_mode_the_seed_refuses_in_one_line(stack):
    """Last in the file: it leaves demo mode off, as the module fixture does."""
    stack.env(APP_MODE=None)
    stack.restart("api", "compass")
    err = stack.cli("demo", "seed", "resources=100", expect=2)
    assert "runs only in demo mode" in err and "Traceback" not in err
