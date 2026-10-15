"""The captain catalog: what can be installed into this Compass (docs/captain_extensions_spec.md §10, M1).

A catalog is one JSON document listing captains published as Python
distributions. Compass reads it from ``COMPASS_CATALOG_URL`` (an https URL or a
file path; ``off`` disables it), by default the copy on the public repository's
main branch, so a new entry shows up without a release. When that cannot be
reached, the copy shipped inside this distribution is used and the answer says
so. Nothing is installed from here: an entry carries the pip requirement that
goes into ``COMPASS_EXTENSIONS``, and the open image installs it on start.

Each entry is annotated against this install: installed (and whether the
catalog has a newer version), installable, incompatible with this core, or
obtained from its vendor (no public package; the paid captains).

    {
      "schema": 1,
      "captains": [
        {"name": "hello", "label": "Hello", "summary": "...", "author": "...",
         "homepage": "https://...", "tier": "open", "tags": ["example"],
         "version": "0.1.0", "requires_core": ">=0.2,<1",
         "pip": "compass-captain-hello @ git+https://...@v0.3.0#subdirectory=..."}
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("compass.catalog")

SCHEMA = 1
DEFAULT_URL = "https://raw.githubusercontent.com/beamreach-ai/compass/main/compass_core/catalog.json"
TTL_SECONDS = 600
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# What counts as a pinned requirement (M6): name==version; name@git+…@<tag or commit>;
# name@https://….whl#sha256=…. A branch is not a pin: what is installed must be what was reviewed.
_PIN_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?===?[A-Za-z0-9.+!_-]+$")
_PIN_GIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*@git\+https://[^@\s]+@(v?\d+(\.\d+)*([a-z0-9.+-]*)|[0-9a-f]{40})(#\S*)?$")
_PIN_WHEEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*@https://\S+\.whl#sha256=[0-9a-f]{64}$")


def pin_problem(pip: str) -> Optional[str]:
    """Why a catalog pip line is not an acceptable pin, or None."""
    if re.search(r"\s", pip):
        return "pip must be one requirement without spaces (name@url, not name @ url)"
    if _PIN_EXACT.match(pip) or _PIN_GIT.match(pip) or _PIN_WHEEL.match(pip):
        return None
    return "pip must pin an exact version (name==1.2.3), a git tag or commit (name@git+https://…@v1.2.3), or a wheel with its sha256"
_TIERS = ("open", "cloud", "enterprise")


@dataclass(frozen=True)
class Entry:
    name: str
    label: str
    summary: str
    tier: str
    version: str
    requires_core: str
    pip: Optional[str] = None            # the COMPASS_EXTENSIONS requirement; None = not publicly installable
    author: Optional[str] = None
    homepage: Optional[str] = None       # where to read about it, or get it when there is no pip line
    source: Optional[str] = None         # the code, for a reader deciding whether to trust it
    tags: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "summary": self.summary, "tier": self.tier,
            "version": self.version, "requires_core": self.requires_core, "pip": self.pip,
            "author": self.author, "homepage": self.homepage, "source": self.source, "tags": list(self.tags),
        }


@dataclass
class Catalog:
    entries: List[Entry]
    source: str                          # the URL or path it came from, or "bundled"
    fetched_at: float
    error: Optional[str] = None          # why the configured source was not used, when it was not
    rejected: Dict[str, str] = field(default_factory=dict)  # entry name (or index) -> why it was dropped


def _entry_errors(raw: Dict[str, Any]) -> List[str]:
    from compass_core.captains import _specifier

    problems: List[str] = []
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(f"name {name!r} is not lower_snake_case")
    for key in ("label", "summary", "version"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            problems.append(f"{key} is required")
    if raw.get("tier") not in _TIERS:
        problems.append(f"tier {raw.get('tier')!r} is not one of {_TIERS}")
    try:
        _specifier(str(raw.get("requires_core") or ""))
    except ValueError as exc:
        problems.append(f"requires_core: {exc}")
    pip = raw.get("pip")
    if pip is not None and (not isinstance(pip, str) or not pip.strip()):
        problems.append("pip must be a requirement string or null")
    elif isinstance(pip, str):
        problem = pin_problem(pip)
        if problem:
            problems.append(problem)
    if pip is None and not raw.get("homepage"):
        problems.append("an entry without a pip line needs a homepage")
    try:
        from packaging.version import Version

        Version(str(raw.get("version") or ""))
    except Exception:  # noqa: BLE001
        problems.append(f"version {raw.get('version')!r} is not a PEP 440 version")
    return problems


def parse(document: Any, *, source: str) -> Catalog:
    """A catalog from its JSON document; invalid entries are dropped and reported, not fatal."""
    if not isinstance(document, dict) or not isinstance(document.get("captains"), list):
        raise ValueError("a catalog is an object with a 'captains' list")
    if int(document.get("schema") or 0) > SCHEMA:
        raise ValueError(f"catalog schema {document.get('schema')} is newer than this Compass understands ({SCHEMA})")
    entries: List[Entry] = []
    rejected: Dict[str, str] = {}
    seen = set()
    for index, raw in enumerate(document["captains"]):
        key = str(raw.get("name") if isinstance(raw, dict) else index)
        if not isinstance(raw, dict):
            rejected[key] = "not an object"
            continue
        problems = _entry_errors(raw)
        if not problems and raw["name"] in seen:
            problems = ["listed twice"]
        if problems:
            rejected[key] = "; ".join(problems)
            continue
        seen.add(raw["name"])
        entries.append(Entry(
            name=raw["name"], label=raw["label"], summary=raw["summary"], tier=raw["tier"],
            version=str(raw["version"]), requires_core=str(raw.get("requires_core") or ""),
            pip=raw.get("pip"), author=raw.get("author"), homepage=raw.get("homepage"), source=raw.get("source"),
            tags=tuple(str(t) for t in raw.get("tags") or ()),
        ))
    return Catalog(entries=entries, source=source, fetched_at=time.time(), rejected=rejected)


# ── loading ─────────────────────────────────────────────────────────────────

def configured_source() -> Optional[str]:
    """``COMPASS_CATALOG_URL``, the default URL when unset, None when set to ``off``."""
    raw = (os.getenv("COMPASS_CATALOG_URL") or "").strip()
    if raw.lower() in ("off", "none", "false", "0"):
        return None
    return raw or DEFAULT_URL


def _read(source: str, *, timeout: float) -> Any:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"Accept": "application/json", "User-Agent": "compass-catalog/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the URL is the operator's
            return json.loads(response.read().decode("utf-8"))
    path = source[len("file://"):] if source.startswith("file://") else source
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        return json.load(handle)


def bundled() -> Catalog:
    """The catalog shipped inside this distribution (what main had at release time)."""
    text = resources.files("compass_core").joinpath("catalog.json").read_text(encoding="utf-8")
    return parse(json.loads(text), source="bundled")


_cache: Dict[str, Tuple[float, Catalog]] = {}
_lock = threading.Lock()


def load(*, refresh: bool = False, timeout: float = 5.0) -> Optional[Catalog]:
    """The catalog this install offers; None when the catalog is turned off.

    Fetched at most every ``TTL_SECONDS``; a source that cannot be read or parsed
    falls back to the bundled copy, with the reason in ``error``."""
    source = configured_source()
    if source is None:
        return None
    with _lock:
        cached = _cache.get(source)
        if cached and not refresh and time.time() - cached[0] < TTL_SECONDS:
            return cached[1]
    try:
        catalog = parse(_read(source, timeout=timeout), source=source)
    except Exception as exc:  # noqa: BLE001 - an unreachable or bad catalog never breaks the page
        logger.warning("captain catalog %s unavailable (%s); using the bundled copy", source, exc)
        catalog = bundled()
        catalog.error = f"{source}: {exc}"
    with _lock:
        _cache[source] = (time.time(), catalog)
    return catalog


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()


# ── against this install ────────────────────────────────────────────────────

def env_line(entry: Entry) -> Optional[str]:
    return f"COMPASS_EXTENSIONS={entry.pip}" if entry.pip else None


def annotate(entry: Entry, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The entry plus what it means here: ``status`` is one of installed, update,
    install, incompatible, vendor; ``licensed`` says whether this workspace may run it."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    from compass_core import captains as registry
    from compass_core import licence as _licence

    installed = registry.discover().get(entry.name)
    compatible = SpecifierSet(entry.requires_core or "").contains(registry.core_version(), prereleases=True)
    licensed = _licence.licence_for(config).is_licensed(SimpleNamespace(name=entry.name, tier=entry.tier))
    newer = False
    if installed is not None:
        try:
            newer = Version(entry.version) > Version(installed.version)
        except InvalidVersion:
            newer = False
    if installed is not None:
        status = "update" if newer and compatible and entry.pip else "installed"
    elif not compatible:
        status = "incompatible"
    elif not entry.pip:
        status = "vendor"
    else:
        status = "install"
    from compass_core import extensions

    requested = next((r for r in extensions.requested() if r.get("name") == entry.name), None)
    awaiting = bool(requested) and (installed is None or (requested.get("version") and requested.get("version") != installed.version))
    managed = extensions.managed()
    return {
        **entry.to_dict(),
        "status": status,
        "installed_version": installed.version if installed is not None else None,
        "compatible": compatible,
        "licensed": licensed,
        "env_line": env_line(entry),
        # what the page can do here: install from the page (or only hand over the line),
        # whether it waits for a restart, whether it can be removed from the page
        "installable_from_page": managed and status in ("install", "update") and bool(entry.pip) and extensions.allowed(entry.name),
        "allowed": extensions.allowed(entry.name),
        "requested": bool(requested),
        "pending_restart": awaiting,
        "removable": managed and bool(requested),
    }


def search(query: Optional[str], entries: List[Entry]) -> List[Entry]:
    terms = [t for t in (query or "").lower().split() if t]
    if not terms:
        return list(entries)

    def text(e: Entry) -> str:
        return " ".join([e.name, e.label, e.summary, e.author or "", " ".join(e.tags)]).lower()

    return [e for e in entries if all(t in text(e) for t in terms)]


def listing(*, query: Optional[str] = None, config: Optional[Dict[str, Any]] = None, refresh: bool = False) -> Dict[str, Any]:
    """What ``GET /api/captains/catalog`` and ``compass catalog`` return."""
    from compass_core import captains as registry

    catalog = load(refresh=refresh)
    if catalog is None:
        return {"enabled": False, "core_version": registry.core_version(), "source": None, "entries": [], "error": None, "rejected": {}}
    return {
        "enabled": True,
        "core_version": registry.core_version(),
        "source": catalog.source,
        "fetched_at": catalog.fetched_at,
        "error": catalog.error,
        "rejected": catalog.rejected,
        "entries": [annotate(e, config=config) for e in search(query, catalog.entries)],
    }
