"""`compass validate`: the CI checks on an author's project (spec §10, M5)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from compass_core import captains
from compass_core.captain_validate import validate

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "captain-hello"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """validate() imports the project and registers its commands: undo both, so
    later tests see neither the copies made here nor their commands."""
    import sys

    from compass_core import dispatch

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(dispatch, "_registry", dict(dispatch._registry))
    captains.reset_for_tests()
    yield
    for name in [m for m in sys.modules if m == "captains.hello" or m.startswith("captains.hello.")]:
        del sys.modules[name]
    captains.reset_for_tests()


def test_the_example_passes():
    report = validate(str(EXAMPLE))
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]
    assert report["captains"] == ["hello"]


def test_problems_are_named(tmp_path):
    project = tmp_path / "mine"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"))
    text = (project / "pyproject.toml").read_text().replace('version = "0.1.0"', 'version = "0.9.0"')
    (project / "pyproject.toml").write_text(text)
    routes = (project / "captains" / "hello" / "routes.py").read_text().replace('@router.get("/api/hello")', '@router.get("/api/elsewhere")')
    (project / "captains" / "hello" / "routes.py").write_text(routes)
    report = validate(str(project))
    failed = {c["check"]: c["detail"] for c in report["checks"] if not c["ok"]}
    assert not report["ok"]
    assert failed["hello: pyproject version matches the manifest"] == "pyproject 0.9.0, manifest 0.1.0"
    assert "/api/elsewhere" in failed["hello: router captains.hello.routes:router routes are under its route_prefixes"]


def test_a_project_without_entry_points(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1"\n')
    report = validate(str(tmp_path))
    assert not report["ok"] and report["checks"][-1]["check"] == "declares compass.captains entry points"
