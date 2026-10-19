"""The extensions installed into this Compass (docs/captain_extensions_spec.md §10, M2–M3).

Installed code belongs to the process, not to a workspace, so the record lives
on the extensions volume (``COMPASS_EXTENSIONS_DIR``, ``/compass/extensions``
in the image):

    requested.json   the extensions installed from the Captains page: [{name, pip, version, requested_at, by}]
    installed.json   what the last sync installed: {"requirements": [...], "core": "0.4.0", "at": ...}
    site/            the pip target; the image puts it on PYTHONPATH
    wheels/          local wheels pip may install from (--find-links)
    restart          a restart request: the time it was asked for

The same volume carries the update files ``compass_core.updates`` and the
updater service exchange; ``read_json``, ``write_json`` and ``lock`` are the
helpers both use.

``sync()`` runs from the image's entrypoint before ``compass`` starts. It
installs the union of ``COMPASS_EXTENSIONS`` and requested.json. When that set
changed, or the core version the set was built against is not the one running
(a Compass update: compiled dependencies and manifests are read against the
new interpreter), it installs into a fresh directory and swaps it in, so
removing an entry or changing its pin rebuilds ``site/`` (uninstall and upgrade
are the same path), and a failed install leaves the previous ``site/`` in place.
A file lock keeps the API and the daemon containers, which start together,
from installing at the same time.

Requirements are pip specifiers without spaces (``name==1.0``,
``name@git+https://…``), separated by spaces, commas or newlines.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("compass.extensions")

_SPLIT_RE = re.compile(r"[\s,]+")


class ExtensionsError(RuntimeError):
    """A refusal the API passes to the client. ``public_detail`` is the only text
    that leaves the process: written here, never derived from another exception."""

    def __init__(self, code: str, detail: str, *, status: int = 409):
        self.code = code
        self.status = status
        self.public_detail = detail
        super().__init__(detail)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.public_detail}


# ── where ───────────────────────────────────────────────────────────────────

def directory() -> Optional[Path]:
    """The extensions volume, or None when this install does not manage its own
    extensions (no ``COMPASS_EXTENSIONS_DIR``: a checkout, the product's images)."""
    raw = (os.getenv("COMPASS_EXTENSIONS_DIR") or "").strip()
    return Path(raw) if raw else None


def managed() -> bool:
    """True when the Captains page may install: an extensions volume, and the
    operator has not turned page installs off (``COMPASS_CATALOG_INSTALL=off``)."""
    if (os.getenv("COMPASS_CATALOG_INSTALL") or "").strip().lower() in ("off", "0", "false", "no"):
        return False
    return directory() is not None


def allowed(name: str) -> bool:
    """The operator's allow-list for page installs: ``COMPASS_CATALOG_ALLOW`` (names,
    comma or space separated); unset means every catalog captain may be installed."""
    raw = (os.getenv("COMPASS_CATALOG_ALLOW") or "").strip()
    return not raw or name in split(raw)


def self_restart() -> bool:
    """True when the processes may exit to be restarted: the shipped compose file
    runs them with ``restart: unless-stopped`` and sets ``COMPASS_SELF_RESTART=1``."""
    return (os.getenv("COMPASS_SELF_RESTART") or "").strip().lower() in ("1", "true", "yes", "on")


def _need_dir() -> Path:
    path = directory()
    if path is None:
        raise ExtensionsError(
            "extensions_unmanaged",
            "This Compass does not install extensions from the page: add the captain's line to COMPASS_EXTENSIONS and restart.",
        )
    return path


# ── the record ──────────────────────────────────────────────────────────────

def split(raw: Optional[str]) -> List[str]:
    return [part for part in _SPLIT_RE.split(raw or "") if part]


def from_env() -> List[str]:
    return split(os.getenv("COMPASS_EXTENSIONS"))


def read_json(path: Path, default: Any) -> Any:
    """The JSON in ``path``, or ``default`` when it is missing or unreadable."""
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        logger.warning("%s is unreadable (%s); treating it as empty", path, exc)
        return default


def write_json(path: Path, value: Any) -> None:
    """Write ``value`` as JSON atomically (a reader never sees a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def requested() -> List[Dict[str, Any]]:
    path = directory()
    if path is None:
        return []
    rows = read_json(path / "requested.json", [])
    return [r for r in rows if isinstance(r, dict) and r.get("name") and r.get("pip")] if isinstance(rows, list) else []


def _installed_record() -> Dict[str, Any]:
    path = directory()
    record = read_json(path / "installed.json", {}) if path is not None else {}
    return record if isinstance(record, dict) else {}


def installed() -> List[str]:
    reqs = _installed_record().get("requirements")
    return [str(r) for r in reqs] if isinstance(reqs, list) else []


def installed_core() -> Optional[str]:
    """The core version ``site/`` was built against (None before the first sync, or
    for a record written before this field existed)."""
    core = _installed_record().get("core")
    return str(core) if core else None


def wanted() -> List[str]:
    """What should be installed: COMPASS_EXTENSIONS, then the page's requests, each once."""
    out: List[str] = []
    for req in from_env() + [str(r["pip"]) for r in requested()]:
        if req not in out:
            out.append(req)
    return out


def pending() -> bool:
    """True when a restart would change what is installed."""
    return directory() is not None and sorted(wanted()) != sorted(installed())


@contextmanager
def lock(path: Path) -> Iterator[None]:
    """One writer at a time on the volume: the API and the daemon start together."""
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def request_install(name: str, pip: str, *, version: Optional[str] = None, by: Optional[str] = None) -> List[Dict[str, Any]]:
    """Record an install (or, for a name already recorded, a new pin: an upgrade)."""
    if not pip or _SPLIT_RE.search(pip):
        raise ExtensionsError("invalid_requirement", f"{pip!r} is not one pip requirement without spaces", status=422)
    path = _need_dir()
    with lock(path):
        rows = [r for r in requested() if r.get("name") != name]
        rows.append({"name": name, "pip": pip, "version": version, "requested_at": time.time(), "by": by})
        write_json(path / "requested.json", rows)
    return rows


def request_uninstall(name: str) -> List[Dict[str, Any]]:
    path = _need_dir()
    with lock(path):
        rows = requested()
        if not any(r.get("name") == name for r in rows):
            raise ExtensionsError(
                "not_installed_from_page",
                f"{name!r} was not installed from the Captains page; remove it from COMPASS_EXTENSIONS to uninstall it.",
                status=404,
            )
        rows = [r for r in rows if r.get("name") != name]
        write_json(path / "requested.json", rows)
    return rows


def source_of(name: str) -> Optional[str]:
    """How an installed captain got here: "page", "env" (COMPASS_EXTENSIONS) or None (shipped)."""
    if any(r.get("name") == name for r in requested()):
        return "page"
    from compass_core import captains

    manifest = captains.discover().get(name)
    if manifest is None:
        return None
    site = directory() / "site" if directory() else None
    module = (manifest.commands or "").split(":")[0] or None
    if site and module:
        try:
            import importlib.util

            spec = importlib.util.find_spec(module)
            if spec and spec.origin and str(site) in spec.origin:
                return "env"
        except (ImportError, ValueError):
            return None
    return None


# ── restart ─────────────────────────────────────────────────────────────────

def request_restart() -> float:
    path = _need_dir()
    path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (path / "restart").write_text(str(now), encoding="utf-8")
    return now


def restart_requested_after(started_at: float) -> bool:
    path = directory()
    if path is None:
        return False
    try:
        return float((path / "restart").read_text(encoding="utf-8").strip() or 0) > started_at
    except (OSError, ValueError):
        return False


# ── sync (the entrypoint) ───────────────────────────────────────────────────

def sync(*, pip: Optional[List[str]] = None) -> Dict[str, Any]:
    """Make ``site/`` hold exactly what is wanted. Returns what happened; never raises
    for a failed install (the previous site stays, the error is reported)."""
    path = directory()
    if path is None:
        return {"changed": False, "reason": "no COMPASS_EXTENSIONS_DIR"}
    from compass_core import __version__ as core

    with lock(path):
        want = wanted()
        have = installed()
        site = path / "site"
        same_core = installed_core() == core or not want
        if sorted(want) == sorted(have) and same_core and (site.exists() or not want):
            return {"changed": False, "requirements": want}
        if want and not same_core and sorted(want) == sorted(have):
            logger.info("rebuilding extensions for compass %s (built against %s)", core, installed_core() or "an older version")
        fresh = path / "site.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        if want:
            wheels = path / "wheels"
            command = list(pip or [sys.executable, "-m", "pip"]) + [
                "install", "--quiet", "--no-cache-dir", "--disable-pip-version-check", "--target", str(fresh),
            ]
            if wheels.is_dir():
                command += ["--find-links", str(wheels)]
            command += want
            logger.info("installing extensions: %s", " ".join(want))
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                shutil.rmtree(fresh, ignore_errors=True)
                error = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or ["pip failed"]
                logger.error("extension install failed, keeping the previous set: %s", error[0])
                write_json(path / "last_error.json", {"at": time.time(), "requirements": want, "error": error[0]})
                return {"changed": False, "requirements": have, "error": error[0]}
        old = path / "site.old"
        shutil.rmtree(old, ignore_errors=True)
        if site.exists():
            site.replace(old)
        fresh.replace(site)
        shutil.rmtree(old, ignore_errors=True)
        write_json(path / "installed.json", {"requirements": want, "core": core, "at": time.time()})
        (path / "last_error.json").unlink(missing_ok=True)
        return {"changed": True, "requirements": want}


def last_error() -> Optional[Dict[str, Any]]:
    path = directory()
    if path is None:
        return None
    record = read_json(path / "last_error.json", None)
    return record if isinstance(record, dict) else None


def state() -> Dict[str, Any]:
    """What GET /api/captains/extensions and the Captains page show."""
    return {
        "managed": managed(),
        "self_restart": self_restart(),
        "requested": requested(),
        "from_env": from_env(),
        "installed": installed(),
        "pending_restart": pending(),
        "last_error": last_error(),
        "allow_list": split(os.getenv("COMPASS_CATALOG_ALLOW")) or None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="compass: %(message)s")
    if args[:1] == ["sync"]:
        result = sync()
        if result.get("error"):
            print(f"compass: extensions not changed: {result['error']}", file=sys.stderr)
        return 0  # a failed install never keeps Compass from starting
    if args[:1] == ["state"]:
        print(json.dumps(state(), indent=2, default=str))
        return 0
    print("usage: python -m compass_core.extensions sync|state", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
