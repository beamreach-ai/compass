"""4.15 The inbox (docs/e2e.md): everything that needs a person as one list, the counts on
the tabs and the total, a tab's Needs you view, acting from the feed, the whole feed."""

from __future__ import annotations

import time


def _inbox(stack, **params):
    r = stack.api.get("/api/inbox", params={"fresh": "true", **params})
    assert r.status_code == 200, r.text
    return r.json()


def _failed_job(stack):
    failed = stack.run_job("mapper", "export", {"format": "nonsense", "out": "/compass/map/inbox"})
    assert failed["status"] == "failed" and failed.get("error"), failed
    return failed


def test_inb_1_a_failed_job_and_a_missing_model_are_items_with_their_actions(stack):
    failed = _failed_job(stack)
    feed = _inbox(stack)
    by_id = {i["id"]: i for i in feed["items"]}
    item = by_id[f"job.failed:{failed['job_id']}"]
    assert item["verb"] == "fix" and item["severity"] == "urgent" and item["tab"] == "queue"
    assert item["title"] == "mapper.export failed" and failed["error"][:30] in item["summary"]
    assert [a["key"] for a in item["actions"]] == ["retry", "acknowledge", "open"]
    assert item["actions"][0]["path"] == f"/api/jobs/{failed['job_id']}/restart"
    assert feed["counts"]["by_tab"]["queue"] >= 1 and feed["counts"]["urgent"] >= 1
    # the stack runs without a model: an info item on the Settings tab, newest first means it is last
    model = by_id.get("setup.incomplete:model")
    assert model and model["severity"] == "info" and model["tab"] == "settings"
    assert feed["items"][-1]["id"] == "setup.incomplete:model"
    assert [i["since"] for i in feed["items"]] == sorted((i["since"] for i in feed["items"]), reverse=True)
    assert [i["id"] for i in _inbox(stack, tab="queue")["items"]] == [i["id"] for i in feed["items"] if i["tab"] == "queue"]
    status = stack.api.get("/api/status").json()["inbox"]
    assert status["line"].startswith("Needs you: ") and status["counts"]["total"] == feed["counts"]["total"]


def test_inb_2_the_sidebar_counts_the_tab_and_its_needs_you_view_lists_the_item(stack, page):
    failed = _failed_job(stack)
    page.open("/workspace?tab=queue")
    nav = page.locator("aside nav")
    nav.locator("[data-inbox-count]").first.wait_for()
    total = page.locator("aside [data-inbox-total]").first
    assert int(total.inner_text()) >= 1
    page.open("/workspace?tab=queue&view=needs_you")
    row = page.locator(f"[data-inbox-item='job.failed:{failed['job_id']}']")
    row.wait_for()
    assert "mapper.export failed" in row.inner_text()
    row.get_by_label("Show detail").click()
    page.get_by_text(failed["error"][:30], exact=False).first.wait_for()
    assert nav.get_by_role("link", name="Needs you", exact=True).first.is_visible(), "the view is listed under Queue"


def test_inb_3_acknowledging_from_the_feed_hides_the_item_and_retry_queues_the_job(stack, page):
    failed = _failed_job(stack)
    item_id = f"job.failed:{failed['job_id']}"
    page.open("/workspace?tab=queue&view=needs_you")
    row = page.locator(f"[data-inbox-item='{item_id}']")
    row.wait_for()
    before = _inbox(stack)["counts"]["total"]
    row.get_by_label("More actions").click()
    row.get_by_role("button", name="Acknowledge", exact=True).click()
    page.get_by_role("status").first.wait_for()
    deadline = time.time() + 20
    while time.time() < deadline and item_id in {i["id"] for i in _inbox(stack)["items"]}:
        time.sleep(1)
    after = _inbox(stack)
    assert item_id not in {i["id"] for i in after["items"]} and after["counts"]["total"] == before - 1
    hidden = [i for i in _inbox(stack, include_snoozed="true")["items"] if i["id"] == item_id]
    assert hidden and hidden[0]["snooze"]
    assert stack.api.delete(f"/api/inbox/{item_id}/snooze").status_code == 200
    assert item_id in {i["id"] for i in _inbox(stack)["items"]}
    # retry from the feed: the same bad payload fails again, as a new item
    page.open("/workspace?tab=queue&view=needs_you")
    page.locator(f"[data-inbox-item='{item_id}']").get_by_role("button", name="Retry", exact=True).click()
    page.get_by_role("status").first.wait_for()
    deadline = time.time() + 120
    while time.time() < deadline:
        newest = stack.api.get("/api/jobs", params={"limit": 5}).json()[0]
        if newest["job_id"] != failed["job_id"] and newest["status"] in ("failed", "completed"):
            break
        time.sleep(2)
    assert newest["job_id"] != failed["job_id"] and newest["status"] == "failed"
    ids = {i["id"] for i in _inbox(stack)["items"]}
    assert f"job.failed:{newest['job_id']}" in ids and item_id not in ids, "the newest failure of the command is the item"


def test_inb_4_the_whole_feed_opens_from_the_total_and_the_phone_shows_it_on_the_menu(stack, page, phone_page):
    _failed_job(stack)
    page.open("/workspace?tab=maps")
    page.locator("aside [data-inbox-total]").first.click()
    page.wait_for_url("**/workspace?tab=inbox")
    page.get_by_role("heading", name="Needs you", exact=True).wait_for()
    feed = page.locator("[data-inbox-feed='all']")
    feed.locator("[data-inbox-item]").first.wait_for()
    kinds = feed.locator("[data-inbox-item]").evaluate_all("rows => rows.map(r => r.dataset.inboxKind)")
    assert kinds[0] == "job.failed" and kinds[-1] == "setup.incomplete", kinds
    page.get_by_role("button", name="Fix", exact=False).click()
    assert all(k == "job.failed" for k in feed.locator("[data-inbox-item]").evaluate_all("rows => rows.map(r => r.dataset.inboxKind)"))
    phone_page.open("/workspace?tab=maps")
    badge = phone_page.locator("button[aria-label='Open menu'] [data-inbox-total]")
    badge.wait_for()
    assert int(badge.inner_text()) >= 1
