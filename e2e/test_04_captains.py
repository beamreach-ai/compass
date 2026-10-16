"""4.5 Captains and extensions (docs/e2e.md): on and off, dependencies, configuration,
the catalog, installing from the page, and the same through the `compass` command."""

from __future__ import annotations

import time

import pytest

SHIPPED = {"mapper", "librarian", "honeypot", "iac", "prs"}
HELLO = {"name": "hello", "label": "Hello", "summary": "Says hello: the end-to-end suite's example captain.", "tier": "open",
         "tags": ["example"], "version": "0.1.0", "requires_core": ">=0.2,<1", "pip": "compass-captain-hello==0.1.0"}
BROKEN = {**HELLO, "name": "broken", "label": "Broken", "summary": "A pin that does not exist.", "pip": "compass-captain-broken==9.9.9"}


@pytest.fixture(scope="module", autouse=True)
def catalog(stack):
    stack.fakes.put("/_control/catalog", json={"schema": 1, "captains": [HELLO, BROKEN]}).raise_for_status()


def _captains(stack):
    return {c["name"]: c for c in stack.api.get("/api/captains").json()["captains"]}


def _settings(page):
    page.open("/workspace?tab=settings&view=captains", wait_for="Turn a captain off")
    return page.locator("#settings-captains ~ section").first


def _row(page, label):
    """A captain's row on the Captains page (not the sidebar's tab of the same name)."""
    return page.locator("#settings-captains ~ section li", has=page.get_by_text(label, exact=True)).first


def _restart_from_page(page, stack):
    page.get_by_role("button", name="Restart Compass").click()
    page.get_by_text("Restarting Compass", exact=False).wait_for()
    page.wait_for_event("load", timeout=300_000)  # the banner reloads the page when Compass is back
    stack.wait_healthy()
    stack._api = None


def test_cap_1_the_installed_list_shows_the_shipped_captains_on(stack, page):
    captains = _captains(stack)
    assert SHIPPED <= set(captains) and all(captains[n]["active"] for n in SHIPPED)
    _settings(page)
    switches = page.locator("#settings-captains ~ section [role=switch]")
    assert switches.count() == 5
    assert all(switches.nth(i).get_attribute("aria-checked") == "true" for i in range(5))


def test_cap_2_a_captain_turned_off_loses_its_tab_and_its_work_until_it_is_on_again(stack, page):
    _settings(page)
    switch = _row(page, "Code-as-cloud").get_by_role("switch")
    switch.click()
    page.wait_for_function("() => document.querySelector('[aria-label=\"Code-as-cloud off\"]') !== null")
    assert not _captains(stack)["iac"]["active"]
    refused = stack.api.post("/api/jobs", json={"captain": "iac", "command": "import", "payload": {}})
    assert refused.status_code in (403, 409) and "off" in refused.text.lower()
    page.reload()
    assert page.locator("aside nav").get_by_role("link", name="IAC", exact=True).count() == 0
    _row(page, "Code-as-cloud").get_by_role("switch").click()
    page.wait_for_function("() => document.querySelector('[aria-label=\"Code-as-cloud on\"]') !== null")
    assert _captains(stack)["iac"]["active"]
    page.reload()
    page.locator("aside nav").get_by_role("link", name="IAC", exact=True).wait_for()


def test_cap_3_dependencies_are_asked_about_and_switched_together(stack, page):
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    _settings(page)
    _row(page, "Infra map").get_by_role("switch").click()  # Decoys, Code-as-cloud and the Librarian need it
    page.wait_for_function("() => document.querySelector('[aria-label=\"Infra map off\"]') !== null")
    assert dialogs and "depend" in dialogs[-1]
    captains = _captains(stack)
    assert not any(captains[n]["active"] for n in ("mapper", "honeypot", "iac", "librarian"))

    _row(page, "Decoys").get_by_role("switch").click()  # needs the map: asks to turn it on too
    page.wait_for_function("() => document.querySelector('[aria-label=\"Decoys on\"]') !== null")
    assert "turned on" in dialogs[-1]
    for name in ("iac", "librarian"):
        stack.api.post(f"/api/captains/{name}/enable", json={"with_dependencies": True}).raise_for_status()
    assert all(c["active"] for n, c in _captains(stack).items() if n in SHIPPED)


def test_cap_5_the_catalog_lists_what_can_be_installed_and_search_filters_it(stack, page):
    listing = stack.api.get("/api/captains/catalog", params={"refresh": True}).json()
    assert listing["source"].endswith("/catalog.json") and listing["error"] is None
    assert {e["name"]: e["status"] for e in listing["entries"]} == {"hello": "install", "broken": "install"}
    _settings(page)
    page.get_by_role("tab", name="Available").click()
    page.get_by_text("Says hello: the end-to-end suite's example captain.").wait_for()
    page.get_by_placeholder("Search captains").fill("pin that does not")
    page.wait_for_timeout(600)
    assert page.get_by_text("Says hello: the end-to-end suite's example captain.").count() == 0
    page.get_by_text("A pin that does not exist.").wait_for()


def test_cap_6_installing_from_the_page_and_restarting_brings_the_captain_and_its_tab(stack, page):
    _settings(page)
    page.get_by_role("tab", name="Available").click()
    _row(page, "Hello").get_by_role("button", name="Install").click()
    page.get_by_text("Captain changes are waiting for a restart", exact=False).wait_for()
    _restart_from_page(page, stack)
    assert _captains(stack)["hello"]["active"] is True
    page.open("/workspace?tab=hello")
    page.locator("aside nav").get_by_role("link", name="Hello", exact=True).wait_for()
    job = stack.run_job("hello", "greet", {"name": "e2e"})
    assert job["status"] == "completed" and job["result_summary"]["greeting"].endswith(", e2e")


def test_cap_4_a_row_click_opens_the_configuration_which_validates_and_masks_secrets(stack, page):
    bad = stack.api.put("/api/captains/hello/config", json={"greeting": 42})
    assert bad.status_code == 422 and "greeting" in bad.json()["detail"]
    saved = stack.api.put("/api/captains/hello/config", json={"greeting": "ahoy", "api_key": "sk-e2e-secret-value"})
    assert saved.status_code == 200 and saved.json()["config"]["api_key"].startswith("…") and "sk-e2e" not in saved.text
    again = stack.api.put("/api/captains/hello/config", json={"api_key": saved.json()["config"]["api_key"]})
    assert again.status_code == 200
    assert stack.run_job("hello", "greet", {"name": "e2e"})["result_summary"]["greeting"] == "ahoy, e2e"
    _settings(page)
    _row(page, "Hello").get_by_text("Says hello", exact=False).click()
    page.get_by_label("greeting", exact=False).first.wait_for()
    assert page.get_by_label("greeting", exact=False).first.input_value() == "ahoy"


def test_cap_7_uninstalling_from_the_page_removes_it_after_the_restart_and_keeps_its_data(stack, page):
    page.on("dialog", lambda dialog: dialog.accept())
    _settings(page)
    _row(page, "Hello").get_by_role("button", name="Uninstall").click()
    page.get_by_text("Captain changes are waiting for a restart", exact=False).wait_for()
    _restart_from_page(page, stack)
    assert "hello" not in _captains(stack)
    assert page.locator("aside nav").get_by_role("link", name="Hello", exact=True).count() == 0
    config = stack.cli("captains")
    assert "hello" not in {c["name"] for c in config["captains"]}
    assert '"greeting": "ahoy"' in stack.read("config/local.json"), "its configuration stays"


def test_cap_8_a_failed_install_keeps_the_previous_set_and_says_why(stack, page):
    stack.api.post("/api/captains/install", json={"name": "broken"}).raise_for_status()
    _settings(page)
    # the install fails, so a restart stays pending and the page does not reload itself
    page.get_by_role("button", name="Restart Compass").click()
    time.sleep(5)
    stack.wait_healthy()
    stack._api = None
    state = stack.api.get("/api/captains/extensions").json()
    assert state["last_error"] and "broken" in state["last_error"]["error"].lower()
    assert set(_captains(stack)) >= SHIPPED and "broken" not in _captains(stack)
    _settings(page)
    page.get_by_text("The last install failed", exact=False).wait_for()
    stack.api.post("/api/captains/uninstall", json={"name": "broken"}).raise_for_status()
    stack.restart("api", "compass")


def test_cap_9_the_operator_can_limit_what_the_page_installs(stack):
    stack.env(COMPASS_CATALOG_INSTALL="off")
    stack.restart("api")
    refused = stack.api.post("/api/captains/install", json={"name": "hello"})
    assert refused.status_code == 409 and refused.json()["code"] == "extensions_unmanaged"
    stack.env(COMPASS_CATALOG_INSTALL=None, COMPASS_CATALOG_ALLOW="someone_else")
    stack.restart("api")
    refused = stack.api.post("/api/captains/install", json={"name": "hello"})
    assert refused.status_code == 403 and refused.json()["code"] == "not_allowed"
    stack.env(COMPASS_CATALOG_ALLOW=None)
    stack.restart("api")


def test_cap_10_the_commands_agree_with_the_page(stack):
    listed = {c["name"]: c for c in stack.cli("captains")["captains"]}
    assert {n: c["active"] for n, c in listed.items()} == {n: c["active"] for n, c in _captains(stack).items()}
    assert stack.cli("disable", "captain=librarian")["changed"] == ["librarian"]
    assert _captains(stack)["librarian"]["active"] is False
    assert stack.cli("enable", "captain=librarian")["changed"] == ["librarian"]
    shown = stack.cli("configure", "captain=prs")
    assert shown["captain"] == "prs" and shown["section"]
    catalog = stack.cli("catalog", "refresh=true")
    assert {e["name"] for e in catalog["entries"]} == {"hello", "broken"}
    installed = stack.cli("install", "captain=hello")
    assert installed["restart_required"] is True
    assert stack.cli("uninstall", "captain=hello")["captain"] == "hello"


def test_cap_11_validate_passes_the_example_and_fails_a_broken_copy_with_the_reason(stack):
    report = stack.cli("validate", "path=/compass/extensions/captain-hello-src", timeout=600)
    assert report["ok"] is True
    manifest = "extensions/captain-hello-src/captains/hello/manifest.py"
    original = stack.read(manifest)
    try:
        stack.write(manifest, original.replace('name="hello"', 'name="Hello-Bad"'))
        error = stack.cli("validate", "path=/compass/extensions/captain-hello-src", expect=2, timeout=600)
        assert "captain validation failed" in error and "Traceback" not in error
    finally:
        stack.write(manifest, original)
    time.sleep(0)
