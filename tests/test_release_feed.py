"""The release workflow's script (.github/scripts/release_feed.py): the changelog gate,
the feed it writes to the releases branch, and the compose defaults it bumps."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_feed", ROOT / ".github" / "scripts" / "release_feed.py")
release_feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_feed)

CHANGELOG = """# Changelog

## 0.5.0 — 2026-10-20

Updates from the page.

- one
- two

### After the update

- Reload the Captains page.

## 0.4.0 — 2026-10-15

Captains from the page.
"""


def test_every_release_this_repository_ships_has_a_changelog_section():
    from compass_core import __version__

    text = (ROOT / "CHANGELOG.md").read_text()
    assert release_feed.changelog_section(text, __version__) is not None
    assert release_feed.main(["notes", "9.9.9", "--changelog", str(ROOT / "CHANGELOG.md")]) == 1


def test_the_feed_gets_the_summary_the_notes_and_the_channels(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text(release_feed.changelog_section(CHANGELOG, "0.5.0"))
    feed = tmp_path / "releases.json"
    feed.write_text(json.dumps({"schema": 1, "channels": {"stable": "0.4.0"}, "releases": [{"version": "0.4.0"}, {"version": "0.5.0rc1"}]}))
    assert release_feed.main(["feed", "0.5.0", "--feed", str(feed), "--notes-file", str(notes), "--notes-url", "https://example.com/v0.5.0",
                              "--image", "compass=sha256:aa", "--image", "compass-dashboard=sha256:bb"]) == 0
    written = json.loads(feed.read_text())
    assert written["channels"] == {"stable": "0.5.0", "prerelease": "0.5.0"}
    newest = written["releases"][0]
    assert newest["version"] == "0.5.0" and newest["summary"] == "Updates from the page."
    assert newest["notes"] == ["Reload the Captains page."] and newest["min_from"] == "0.4.0"
    assert newest["images"] == {"compass": "sha256:aa", "compass-dashboard": "sha256:bb"}

    from compass_core import updates

    parsed = updates.parse_feed(written)
    assert parsed.latest("stable") == "0.5.0", "what the script writes is what the check reads"


def test_the_defaults_follow_the_release():
    compose, env = release_feed.bump_defaults((ROOT / "docker-compose.yml").read_text(), (ROOT / ".env.example").read_text(), "0.9.1")
    assert "COMPASS_VERSION:-0.9.1" in compose and "COMPASS_VERSION:-0.4.0" not in compose
    assert "\nCOMPASS_VERSION=0.9.1\n" in env
