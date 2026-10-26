"""4.3 The dashboard shell (docs/e2e.md): the tabs, the sidebar, Settings' sections, the
button colours, the phone layout."""

from __future__ import annotations

import re

from e2e import fixture

OPEN_TABS = {"maps": "Maps", "honeypots": "Decoys", "patches": "Patches", "iac": "IAC", "prs": "Pull requests", "queue": "Queue", "settings": "Settings"}
CLOSED_TABS = ("FinOps", "DevSecOps", "Compliance", "Policies")
# noise a browser logs that is not the page's fault
IGNORED_CONSOLE = re.compile(r"favicon|Download the React DevTools|net::ERR_ABORTED")


def _nav(page):
    return page.locator("aside nav")


def test_ui_1_every_tab_opens_without_an_error_and_the_closed_ones_are_absent(page):
    for tab, label in OPEN_TABS.items():
        page.open(f"/workspace?tab={tab}")
        _nav(page).get_by_role("link", name=label, exact=True).wait_for()
    nav_text = _nav(page).inner_text()
    for closed in CLOSED_TABS:
        assert closed not in nav_text
    errors = [e for e in page.console_errors if not IGNORED_CONSOLE.search(e)]
    assert errors == [], errors


def test_ui_2_the_sidebar_has_settings_at_the_bottom_and_lists_sections_while_open(page):
    page.open("/workspace?tab=honeypots")
    links = _nav(page).get_by_role("link")
    names = [n.strip() for n in links.all_inner_texts()]
    assert names[:4] == ["IAC", "Decoys", "Overview", "Honeytokens"], names
    assert names[-1] == "Settings", "Settings is last"
    settings_box = _nav(page).get_by_role("link", name="Settings", exact=True).last.bounding_box()
    account_box = page.get_by_text("Target account", exact=False).bounding_box()
    others = [_nav(page).get_by_role("link", name=n, exact=True).bounding_box() for n in ("Queue", "Maps", "Pull requests")]
    assert all(settings_box["y"] > box["y"] for box in others) and settings_box["y"] < account_box["y"]

    page.open("/workspace?tab=settings")
    sections = _nav(page).get_by_role("list", name="Settings sections").get_by_role("link").all_inner_texts()
    assert [s.strip() for s in sections] == ["Integrations", "Captains", "Updates"]


def test_ui_3_each_settings_section_link_scrolls_to_its_heading(page):
    page.open("/workspace?tab=settings")
    for section in ("Captains", "Updates", "Integrations"):
        _nav(page).get_by_role("link", name=section, exact=True).click()
        heading = page.locator(f"#settings-{section.lower()}")
        heading.wait_for()
        page.wait_for_timeout(700)  # the smooth scroll
        # in view: near the top, or (the last section, which cannot scroll further) anywhere on screen
        top, bottom, height = heading.evaluate("el => [el.getBoundingClientRect().top, el.getBoundingClientRect().bottom, innerHeight]")
        assert -5 <= top and bottom <= height, f"{section} heading at {top}px of {height}px"


BUTTON_COLOURS = """
() => Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null).map(b => {
  const s = getComputedStyle(b);
  const rgb = c => (c.match(/\\d+/g) || []).slice(0, 4).map(Number);
  const [r, g, bl, a] = rgb(s.backgroundColor);
  const blue = (a === undefined || a > 0.5) && bl > 150 && bl > r + 60 && bl > g + 30;
  return {text: (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 40), background: s.backgroundColor, color: s.color, blue};
}).filter(b => b.blue && b.color !== 'rgb(255, 255, 255)')
"""


def test_ui_4_every_blue_button_has_white_text(page, mapped):
    offenders = {}
    for tab in OPEN_TABS:
        page.open(f"/workspace?tab={tab}")
        page.wait_for_timeout(500)
        bad = page.evaluate(BUTTON_COLOURS)
        if bad:
            offenders[tab] = bad
    assert offenders == {}, offenders


def test_ui_5_on_a_phone_the_menu_is_a_drawer_that_closes(phone_page):
    page = phone_page
    page.open("/workspace?tab=maps")
    drawer = page.locator("aside[aria-label='Sidebar']")
    # closed: off-screen to the left, give or take the scrollbar gutter the page keeps on both edges
    closed = "el => el.getBoundingClientRect().right <= 20"
    menu = page.get_by_role("button", name="Open menu")
    assert drawer.evaluate(closed), "closed at first"
    menu.click()
    page.wait_for_timeout(300)
    assert drawer.evaluate("el => el.getBoundingClientRect().left") >= 0
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert drawer.evaluate(closed), "Escape closes it"
    menu.click()
    page.wait_for_timeout(300)
    drawer.get_by_role("link", name="Queue", exact=True).click()
    page.wait_for_url("**tab=queue**")
    page.wait_for_timeout(300)
    assert drawer.evaluate(closed), "navigating closes it"


def test_ui_6_the_sidebar_shows_the_target_account_and_workspace(page):
    page.open("/workspace?tab=queue")
    footer = page.locator("aside").get_by_text("Target account", exact=False).locator("xpath=../..")
    page.get_by_text(fixture.ACCOUNT, exact=True).first.wait_for()
    text = footer.inner_text()
    assert fixture.ACCOUNT in text and "local" in text
