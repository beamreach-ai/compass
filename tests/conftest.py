"""Every test runs without the network: the update check (compass_core.updates) is off
unless a test turns it on and points it at a file."""

import pytest


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    monkeypatch.setenv("COMPASS_UPDATE_CHECK", "off")
    from compass_core import updates

    updates.reset_for_tests()
    yield
    updates.reset_for_tests()
