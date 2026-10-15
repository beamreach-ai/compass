"""The captain catalog: format, fallback, statuses against this install, search,
and the API route (docs/captain_extensions_spec.md §10, M1)."""

from __future__ import annotations

import json

import pytest

from compass_core import captains, catalog


def _doc(*entries):
    return {"schema": 1, "captains": list(entries)}


def _entry(name="hello", **kw):
    base = {"name": name, "label": name.title(), "summary": "s", "tier": "open", "version": "0.1.0",
            "requires_core": ">=0.1,<1", "pip": f"compass-captain-{name}==0.1.0"}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def fresh():
    catalog.reset_for_tests()
    captains.reset_for_tests()
    yield
    catalog.reset_for_tests()
    captains.reset_for_tests()


def test_the_bundled_catalog_is_valid_and_parses():
    shipped = catalog.bundled()
    assert shipped.rejected == {}, shipped.rejected
    names = [e.name for e in shipped.entries]
    assert "hello" in names
    assert all(e.pip or e.homepage for e in shipped.entries)


def test_invalid_entries_are_dropped_with_the_reason():
    parsed = catalog.parse(_doc(
        _entry("good"),
        _entry("Bad-Name"),
        _entry("nopip", pip=None, homepage=None),
        _entry("badtier", tier="gold"),
        _entry("badspec", requires_core="not a spec"),
        _entry("good"),
    ), source="test")
    assert [e.name for e in parsed.entries] == ["good"]
    assert set(parsed.rejected) == {"Bad-Name", "nopip", "badtier", "badspec", "good"}
    assert "listed twice" in parsed.rejected["good"]
    with pytest.raises(ValueError, match="newer"):
        catalog.parse({"schema": 99, "captains": []}, source="test")


def test_a_file_source_is_read_and_an_unreachable_one_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_doc(_entry("solo"))))
    monkeypatch.setenv("COMPASS_CATALOG_URL", str(path))
    loaded = catalog.load()
    assert [e.name for e in loaded.entries] == ["solo"] and loaded.error is None and loaded.source == str(path)

    catalog.reset_for_tests()
    monkeypatch.setenv("COMPASS_CATALOG_URL", str(tmp_path / "missing.json"))
    fallback = catalog.load()
    assert fallback.source == "bundled" and "missing.json" in fallback.error
    assert any(e.name == "hello" for e in fallback.entries)

    monkeypatch.setenv("COMPASS_CATALOG_URL", "off")
    assert catalog.load() is None
    assert catalog.listing()["enabled"] is False


def test_loads_are_cached_until_refresh(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_doc(_entry("one"))))
    monkeypatch.setenv("COMPASS_CATALOG_URL", str(path))
    assert [e.name for e in catalog.load().entries] == ["one"]
    path.write_text(json.dumps(_doc(_entry("two"))))
    assert [e.name for e in catalog.load().entries] == ["one"], "cached"
    assert [e.name for e in catalog.load(refresh=True).entries] == ["two"]


def test_statuses_against_this_install():
    shipped_version = captains.manifest("prs").version
    entries = catalog.parse(_doc(
        _entry("prs", version=shipped_version),                       # installed, same version
        _entry("iac", version="99.0.0"),                              # installed, catalog newer
        _entry("newbie"),                                             # not installed, installable
        _entry("future", requires_core=">=99"),                       # not for this core
        _entry("paid", tier="cloud", pip=None, homepage="https://x"),  # from its vendor
        _entry("ent", tier="enterprise"),                             # installable, not licensed on free
    ), source="test").entries
    by_name = {e.name: catalog.annotate(e, config={}) for e in entries}
    assert by_name["prs"]["status"] == "installed" and by_name["prs"]["installed_version"] == shipped_version
    assert by_name["iac"]["status"] == "update"
    assert by_name["newbie"]["status"] == "install" and by_name["newbie"]["env_line"] == "COMPASS_EXTENSIONS=compass-captain-newbie==0.1.0"
    assert by_name["future"]["status"] == "incompatible" and by_name["future"]["compatible"] is False
    assert by_name["paid"]["status"] == "vendor" and by_name["paid"]["env_line"] is None
    assert by_name["ent"]["status"] == "install" and by_name["ent"]["licensed"] is False
    assert catalog.annotate(entries[-1], config={"billing": {"plan": "enterprise"}})["licensed"] is True


def test_search_matches_every_word_across_fields():
    entries = catalog.parse(_doc(
        _entry("hello", summary="Says hello", tags=["example"]),
        _entry("drift", label="Drift alarm", summary="Pages on drift", author="Acme"),
    ), source="test").entries
    assert [e.name for e in catalog.search("acme drift", entries)] == ["drift"]
    assert [e.name for e in catalog.search("EXAMPLE", entries)] == ["hello"]
    assert len(catalog.search("", entries)) == 2
    assert catalog.search("nothing matches", entries) == []


def test_the_api_serves_the_catalog_before_the_captain_route(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from compass_api.routes import captains as routes

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_doc(_entry("newbie"), _entry("drift", summary="pages on drift"))))
    monkeypatch.setenv("COMPASS_CATALOG_URL", str(path))
    monkeypatch.setattr(routes, "get_workspace_config", lambda request: {})
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    body = client.get("/api/captains/catalog").json()
    assert body["enabled"] and body["source"] == str(path)
    assert {e["name"]: e["status"] for e in body["entries"]} == {"newbie": "install", "drift": "install"}
    assert [e["name"] for e in client.get("/api/captains/catalog", params={"q": "drift"}).json()["entries"]] == ["drift"]


def test_the_example_captain_is_a_valid_installable_manifest(monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "examples" / "captain-hello"
    monkeypatch.syspath_prepend(str(example))
    manifest = captains.load_reference("captains.hello.manifest:MANIFEST")
    assert manifest.errors() == []
    assert captains.incompatibility(manifest) is None
    entry = next(e for e in catalog.bundled().entries if e.name == "hello")
    assert entry.version == manifest.version and entry.requires_core == manifest.requires_core
    text = (example / "pyproject.toml").read_text()
    assert 'hello = "captains.hello.manifest:MANIFEST"' in text and f'version = "{manifest.version}"' in text
    assert not (example / "captains" / "__init__.py").exists(), "captains is a namespace package"
