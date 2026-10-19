"""Every test runs without the network: the update check (compass_core.updates) and its
release notes are off unless a test turns them on and points them at a file."""

import pytest


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    monkeypatch.setenv("COMPASS_UPDATE_CHECK", "off")
    monkeypatch.setenv("COMPASS_RELEASE_NOTES_URL", "off")
    from compass_core import updates

    updates.reset_for_tests()
    yield
    updates.reset_for_tests()
