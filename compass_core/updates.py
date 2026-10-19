"""Knowing a Compass release is out, and asking for it to be applied (docs/open_updates_spec.md).

The check is one anonymous GET of ``releases.json`` (``COMPASS_UPDATE_URL``;
by default the ``releases`` branch of the public repository, written only by
the release workflow), at most every ``COMPASS_UPDATE_CHECK_HOURS`` hours, with
the version in the user agent and nothing else. ``COMPASS_UPDATE_CHECK=off``
turns it off. An install follows one channel, ``COMPASS_UPDATE_CHANNEL``
(``stable`` or ``prerelease``).

When a release is out, the check also reads the release notes: the RSS feed the
Beamreach website publishes (``COMPASS_RELEASE_NOTES_URL``; ``off`` skips it), on
the same cadence and switch. The notes are optional: a feed that cannot be read
leaves the update check as it was and says why in ``notes_error``.

Applying is the updater service's job (``docker/updater``): it holds the Docker
socket, Compass does not. The two talk through files on the extensions volume
(``compass_core.extensions.directory()``), the narrowest interface there is and
one that works when the API is down:

    update.check.json    the last fetched feed, with its ETag
    update.notes.json    the last fetched release notes (the RSS document), with its ETag
    update.json          a request: {target, digests, requested_at, by, mode, force, wait_minutes}
    update.pending       written by the updater between ``compose up`` and the health check
    update.result.json   what happened: {status, from, to, mode, error, at}
    rollback.json        {to, until}: the version to roll back to, offered for seven days
    status.json          {version, active_jobs, at, by}: written every 30 s by the API and the daemon
    update.policy.json   {auto, window, channel}: the policy, mirrored for the updater
    updater.json         the updater's heartbeat: {at, version}

Without a live updater the state says ``apply: "manual"`` and carries the two
shell commands instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from compass_core import __version__, extensions

logger = logging.getLogger("compass.updates")

SCHEMA = 1
DEFAULT_URL = "https://raw.githubusercontent.com/beamreach-ai/compass/releases/releases.json"
DEFAULT_NOTES_URL = "https://beamreach.ai/compass/releases/feed.xml"
NOTES_NAMESPACE = "https://beamreach.ai/compass/releases/ns/1"
NOTES_MAX_BYTES = 1_000_000
CHANNELS = ("stable", "prerelease")
POLICIES = ("notify", "patch", "minor", "off")
DEFAULT_CHECK_HOURS = 6.0
RETRY_SECONDS = 600            # after a failed fetch, before trying again
UPDATER_HEARTBEAT_SECONDS = 120  # a heartbeat older than this means no updater
ROLLBACK_DAYS = 7
DEFAULT_WAIT_MINUTES = 60
IMAGES = ("compass", "compass-dashboard", "compass-updater")

_OFF = ("off", "0", "false", "no", "none")
_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class UpdatesError(RuntimeError):
    """A refusal the API passes to the client, with a code the page switches on."""

    def __init__(self, code: str, detail: str, *, status: int = 409, **extra: Any):
        self.code = code
        self.status = status
        self.extra = extra
        super().__init__(detail)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": str(self), **self.extra}


# ── what this install is ────────────────────────────────────────────────────

def running_version() -> str:
    """What runs: the distribution's version, never the environment's."""
    return __version__


def pinned_version() -> Optional[str]:
    """``COMPASS_VERSION`` as compose passed it in: what the compose file asked to pull."""
    raw = (os.getenv("COMPASS_VERSION") or "").strip()
    return raw or None


def channel() -> str:
    raw = (os.getenv("COMPASS_UPDATE_CHANNEL") or "").strip().lower()
    return raw if raw in CHANNELS else "stable"


def check_hours() -> float:
    try:
        return max(0.0, float(os.getenv("COMPASS_UPDATE_CHECK_HOURS") or DEFAULT_CHECK_HOURS))
    except ValueError:
        return DEFAULT_CHECK_HOURS


def _version(text: Any) -> Optional[Version]:
    try:
        return Version(str(text))
    except InvalidVersion:
        return None


# ── the policy ──────────────────────────────────────────────────────────────

def policy(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``{"auto", "window", "source"}``: the workspace config (``updates.auto``) wins over
    ``COMPASS_AUTO_UPDATE``; ``notify`` when neither says."""
    section = (config or {}).get("updates") if isinstance((config or {}).get("updates"), dict) else {}
    auto, source = "notify", "default"
    env = (os.getenv("COMPASS_AUTO_UPDATE") or "").strip().lower()
    if env in POLICIES:
        auto, source = env, "env"
    configured = str(section.get("auto") or "").strip().lower()
    if configured in POLICIES:
        auto, source = configured, "config"
    return {"auto": auto, "window": window_text(), "source": source}


def window_text() -> Optional[str]:
    raw = (os.getenv("COMPASS_UPDATE_WINDOW") or "").strip()
    return raw if raw and _WINDOW_RE.match(raw) else None


def in_window(now: Optional[datetime] = None) -> bool:
    """True when automatic applies may run now: inside ``COMPASS_UPDATE_WINDOW``
    (``HH:MM-HH:MM`` in the host's ``TZ``; a window across midnight is fine), or always
    when there is no window."""
    text = window_text()
    if not text:
        return True
    h1, m1, h2, m2 = (int(part) for part in _WINDOW_RE.match(text).groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    moment = now or datetime.now()
    minute = moment.hour * 60 + moment.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def check_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """False when the check is turned off: ``COMPASS_UPDATE_CHECK=off``, or the policy is ``off``."""
    if (os.getenv("COMPASS_UPDATE_CHECK") or "").strip().lower() in _OFF:
        return False
    return policy(config)["auto"] != "off"


def source() -> str:
    raw = (os.getenv("COMPASS_UPDATE_URL") or "").strip()
    return raw or DEFAULT_URL


def notes_source() -> Optional[str]:
    """Where the release notes come from; None when ``COMPASS_RELEASE_NOTES_URL`` is set
    empty or ``off`` (unset is the Beamreach website)."""
    raw = os.getenv("COMPASS_RELEASE_NOTES_URL")
    if raw is None:
        return DEFAULT_NOTES_URL
    raw = raw.strip()
    return None if not raw or raw.lower() in _OFF else raw


def mirror_policy(config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Write the policy where the updater reads it (it refuses an automatic request
    the policy no longer allows). None when there is no volume."""
    path = extensions.directory()
    if path is None:
        return None
    record = {**policy(config), "channel": channel(), "at": time.time()}
    current = extensions.read_json(path / "update.policy.json", None)
    if not isinstance(current, dict) or {k: current.get(k) for k in ("auto", "window", "channel")} != {k: record[k] for k in ("auto", "window", "channel")}:
        extensions.write_json(path / "update.policy.json", record)
    return record


# ── the feed ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Feed:
    releases: Dict[str, Dict[str, Any]]   # version -> entry, valid entries only
    channels: Dict[str, str]
    source: str
    fetched_at: float                     # the last successful fetch (or 304)
    checked_at: float                     # the last attempt
    etag: Optional[str] = None
    error: Optional[str] = None           # why the last attempt failed, when it did
    document: Any = None                  # as fetched, for the cache file

    def latest(self, name: str) -> Optional[str]:
        """The channel's newest version: what the feed says, else the newest listed
        release (prereleases only on the prerelease channel)."""
        named = self.channels.get(name)
        if named and named in self.releases:
            return named
        candidates = [v for v in self.releases if name == "prerelease" or not Version(v).is_prerelease]
        return max(candidates, key=Version) if candidates else None


def parse_feed(document: Any, *, source: str = "test") -> Feed:
    if not isinstance(document, dict) or not isinstance(document.get("releases"), list):
        raise ValueError("a release feed is an object with a 'releases' list")
    if int(document.get("schema") or 0) > SCHEMA:
        raise ValueError(f"release feed schema {document.get('schema')} is newer than this Compass understands ({SCHEMA})")
    releases: Dict[str, Dict[str, Any]] = {}
    for raw in document["releases"]:
        if not isinstance(raw, dict) or _version(raw.get("version")) is None:
            continue
        version = str(Version(str(raw["version"])))
        images = raw.get("images") if isinstance(raw.get("images"), dict) else {}
        releases[version] = {
            "version": version,
            "published_at": raw.get("published_at"),
            "notes_url": raw.get("notes_url"),
            "summary": raw.get("summary") or "",
            "images": {str(k): str(v) for k, v in images.items() if v},
            "min_from": str(raw["min_from"]) if _version(raw.get("min_from")) else None,
            "requires_restart": [str(s) for s in raw.get("requires_restart") or []],
            "notes": [str(n) for n in raw.get("notes") or []],
        }
    channels = {str(k): str(v) for k, v in (document.get("channels") or {}).items() if _version(v)} if isinstance(document.get("channels"), dict) else {}
    now = time.time()
    return Feed(releases=releases, channels=channels, source=source, fetched_at=now, checked_at=now, document=document)


def _get(url: str, *, etag: Optional[str], timeout: float, accept: str, limit: Optional[int] = None) -> Tuple[int, Optional[str], Optional[bytes]]:
    """(HTTP status, ETag, body) of an http(s) URL or a file path; 304 carries no body.
    A body over ``limit`` bytes is refused rather than cut."""
    if url.startswith(("http://", "https://")):
        headers = {"Accept": accept, "User-Agent": f"compass-update/{running_version()}"}
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the URL is the operator's
                status, new_etag = response.status, response.headers.get("ETag")
                body = response.read() if limit is None else response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, etag, None
            raise
    else:
        path = url[len("file://"):] if url.startswith("file://") else url
        with open(os.path.expanduser(path), "rb") as handle:
            status, new_etag = 200, None
            body = handle.read() if limit is None else handle.read(limit + 1)
    if limit is not None and len(body) > limit:
        raise ValueError(f"larger than {limit} bytes")
    return status, new_etag, body


def _fetch(url: str, *, etag: Optional[str], timeout: float) -> Tuple[int, Optional[str], Any]:
    """(HTTP status, ETag, document); 304 carries no document."""
    status, etag, body = _get(url, etag=etag, timeout=timeout, accept="application/json")
    return status, etag, None if body is None else json.loads(body.decode("utf-8"))


_cache: Dict[str, Feed] = {}
_lock = threading.Lock()


def _cache_file() -> Optional[Path]:
    path = extensions.directory()
    return path / "update.check.json" if path is not None else None


def _cached(url: str) -> Optional[Feed]:
    """This process's last copy, or the shared one on the volume when that is newer: the API
    and the daemon share update.check.json, so a check made from the page reaches the daemon."""
    with _lock:
        feed = _cache.get(url)
    file = _cache_file()
    record = extensions.read_json(file, None) if file else None
    if not isinstance(record, dict) or record.get("url") != url:
        return feed
    if feed is not None and float(record.get("checked_at") or 0) <= feed.checked_at:
        return feed
    try:
        feed = parse_feed(record.get("document"), source=url)
    except ValueError:
        return None
    feed = replace(feed, fetched_at=float(record.get("fetched_at") or 0), checked_at=float(record.get("checked_at") or 0),
                   etag=record.get("etag"), error=record.get("error"))
    with _lock:
        _cache[url] = feed
    return feed


def _remember(feed: Feed) -> None:
    with _lock:
        _cache[feed.source] = feed
    file = _cache_file()
    if file:
        try:
            extensions.write_json(file, {"url": feed.source, "etag": feed.etag, "fetched_at": feed.fetched_at,
                                         "checked_at": feed.checked_at, "error": feed.error, "document": feed.document})
        except OSError as exc:
            logger.debug("update check not cached: %s", exc)


def load_feed(*, refresh: bool = False, timeout: float = 5.0) -> Feed:
    """The release feed, fetched at most every ``check_hours()`` (ten minutes after a
    failure); a fetch that fails keeps the last good copy with the reason in ``error``."""
    url = source()
    cached = _cached(url)
    now = time.time()
    if cached and not refresh:
        ttl = check_hours() * 3600 if cached.error is None else RETRY_SECONDS
        if now - cached.checked_at < ttl:
            return cached
    try:
        status, etag, document = _fetch(url, etag=cached.etag if cached else None, timeout=timeout)
        if status == 304 and cached:
            feed = replace(cached, fetched_at=now, checked_at=now, error=None)
        else:
            feed = replace(parse_feed(document, source=url), etag=etag)
    except Exception as exc:  # noqa: BLE001 - an unreachable feed never breaks the page
        logger.warning("release feed %s unavailable (%s)", url, exc)
        if cached:
            feed = replace(cached, checked_at=now, error=f"{url}: {exc}")
        else:
            feed = Feed(releases={}, channels={}, source=url, fetched_at=0.0, checked_at=now, error=f"{url}: {exc}")
    _remember(feed)
    return feed


# ── the release notes ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Notes:
    releases: Dict[str, Dict[str, Any]]   # version -> note
    source: str
    fetched_at: float
    checked_at: float
    etag: Optional[str] = None
    error: Optional[str] = None
    document: Optional[str] = None        # the RSS as fetched, for the cache file


_DOCTYPE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def _text(element: Optional[ElementTree.Element]) -> str:
    return (element.text or "").strip() if element is not None else ""


def _published(text: str) -> Optional[str]:
    """RSS's RFC 822 date as the ISO 8601 releases.json uses; None when it does not parse."""
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_notes(document: str) -> Dict[str, Dict[str, Any]]:
    """The ``compass:*`` elements of the RSS feed, by version. Everything is plain text;
    ``content:encoded`` (HTML for feed readers) is ignored, and a link that is not https is
    dropped. Items without a ``compass:version`` are skipped."""
    if len(document.encode("utf-8")) > NOTES_MAX_BYTES:
        raise ValueError(f"release notes are larger than {NOTES_MAX_BYTES} bytes")
    # no DTD at all: the notes never need one, and refusing it rules out entity expansion
    if _DOCTYPE_RE.search(document):
        raise ValueError("release notes with a DOCTYPE are refused")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ValueError(f"release notes are not XML: {exc}") from None
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("release notes are an RSS document with a channel")
    ns = {"compass": NOTES_NAMESPACE}
    notes: Dict[str, Dict[str, Any]] = {}
    for item in channel.findall("item"):
        version = _version(_text(item.find("compass:version", ns)))
        if version is None:
            continue
        link = _text(item.find("link"))
        features = [{"title": _text(f.find("compass:title", ns)), "body": _text(f.find("compass:body", ns))}
                    for f in item.findall("compass:feature", ns)]
        notes[str(version)] = {
            "version": str(version),
            "title": _text(item.find("title")),
            "headline": _text(item.find("compass:headline", ns)) or None,
            "link": link if link.startswith("https://") else None,
            "published_at": _published(_text(item.find("pubDate"))),
            "summary": _text(item.find("description")),
            "features": [f for f in features if f["title"] or f["body"]],
            "after": [t for t in (_text(a) for a in item.findall("compass:after", ns)) if t],
        }
    return notes


_notes_cache: Dict[str, Notes] = {}


def _notes_file() -> Optional[Path]:
    path = extensions.directory()
    return path / "update.notes.json" if path is not None else None


def _cached_notes(url: str) -> Optional[Notes]:
    """As ``_cached``: this process's copy, or the volume's when that is newer."""
    with _lock:
        notes = _notes_cache.get(url)
    file = _notes_file()
    record = extensions.read_json(file, None) if file else None
    if not isinstance(record, dict) or record.get("url") != url:
        return notes
    if notes is not None and float(record.get("checked_at") or 0) <= notes.checked_at:
        return notes
    try:
        releases = parse_notes(record.get("document") or "") if record.get("document") else {}
    except ValueError:
        return None
    notes = Notes(releases=releases, source=url, fetched_at=float(record.get("fetched_at") or 0), checked_at=float(record.get("checked_at") or 0),
                  etag=record.get("etag"), error=record.get("error"), document=record.get("document"))
    with _lock:
        _notes_cache[url] = notes
    return notes


def _remember_notes(notes: Notes) -> None:
    with _lock:
        _notes_cache[notes.source] = notes
    file = _notes_file()
    if file:
        try:
            extensions.write_json(file, {"url": notes.source, "etag": notes.etag, "fetched_at": notes.fetched_at,
                                         "checked_at": notes.checked_at, "error": notes.error, "document": notes.document})
        except OSError as exc:
            logger.debug("release notes not cached: %s", exc)


def load_notes(*, refresh: bool = False, timeout: float = 5.0) -> Optional[Notes]:
    """The release notes, on the release feed's cadence and with its fallback: a fetch that
    fails keeps the last good copy with the reason in ``error``. None when turned off."""
    url = notes_source()
    if url is None:
        return None
    cached = _cached_notes(url)
    now = time.time()
    if cached and not refresh:
        ttl = check_hours() * 3600 if cached.error is None else RETRY_SECONDS
        if now - cached.checked_at < ttl:
            return cached
    try:
        status, etag, body = _get(url, etag=cached.etag if cached else None, timeout=timeout,
                                  accept="application/rss+xml, application/xml;q=0.9", limit=NOTES_MAX_BYTES)
        if status == 304 and cached:
            notes = replace(cached, fetched_at=now, checked_at=now, error=None)
        else:
            document = (body or b"").decode("utf-8")
            notes = Notes(releases=parse_notes(document), source=url, fetched_at=now, checked_at=now, etag=etag, document=document)
    except Exception as exc:  # noqa: BLE001 - the notes are optional; nothing they do breaks the check
        logger.warning("release notes %s unavailable (%s)", url, exc)
        if cached:
            notes = replace(cached, checked_at=now, error=f"{url}: {exc}")
        else:
            notes = Notes(releases={}, source=url, fetched_at=0.0, checked_at=now, error=f"{url}: {exc}")
    _remember_notes(notes)
    return notes


def notes_between(notes: Notes, running: Version, target: Version, *, prereleases: bool) -> List[Dict[str, Any]]:
    """The notes of every release after ``running`` up to and including ``target``, newest first."""
    window = [(Version(v), note) for v, note in notes.releases.items()
              if running < Version(v) <= target and (prereleases or not Version(v).is_prerelease)]
    return [note for _, note in sorted(window, key=lambda pair: pair[0], reverse=True)]


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _notes_cache.clear()


# ── the state ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpdateState:
    running: str                      # compass_core.__version__
    pinned: Optional[str]             # COMPASS_VERSION as compose passed it (None when not passed)
    channel: str
    latest: Optional[str]             # the channel's newest, or None when the check is off or failed
    available: bool                   # latest > running
    direct: bool                      # running >= latest's min_from
    release: Optional[Dict[str, Any]]  # the latest's entry
    checked_at: Optional[float]
    error: Optional[str]              # why the check did not run, when it did not
    apply: str                        # "updater" | "manual" | "off"
    pinned_differs: bool = False      # the compose file asks for another version than runs
    via: Optional[str] = None         # when not direct: the version to step through first
    releases: List[Dict[str, Any]] = field(default_factory=list)  # newer than running, newest first
    release_notes: List[Dict[str, Any]] = field(default_factory=list)  # running < v <= latest from the notes feed, newest first
    notes_source: Optional[str] = None  # where release_notes came from (None when the notes are off or nothing is offered)
    notes_error: Optional[str] = None   # why the notes could not be read; the check itself is unaffected

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def apply_mode(config: Optional[Dict[str, Any]] = None) -> str:
    if not check_enabled(config):
        return "off"
    return "updater" if updater_alive() else "manual"


def updater_heartbeat() -> Optional[Dict[str, Any]]:
    path = extensions.directory()
    record = extensions.read_json(path / "updater.json", None) if path is not None else None
    return record if isinstance(record, dict) else None


def updater_alive() -> bool:
    beat = updater_heartbeat()
    try:
        return beat is not None and time.time() - float(beat.get("at") or 0) < UPDATER_HEARTBEAT_SECONDS
    except (TypeError, ValueError):
        return False


def _step_through(feed: Feed, running: Version, target: Version) -> Optional[str]:
    """The newest release below ``target`` that ``running`` can reach directly."""
    reachable = []
    for version, entry in feed.releases.items():
        v = Version(version)
        if running < v < target and not v.is_prerelease:
            floor = _version(entry.get("min_from"))
            if floor is None or running >= floor:
                reachable.append(version)
    return max(reachable, key=Version) if reachable else None


def check(*, refresh: bool = False, config: Optional[Dict[str, Any]] = None) -> UpdateState:
    """Where this install stands against the feed; never raises."""
    running = running_version()
    pinned = pinned_version()
    name = channel()
    pinned_differs = bool(pinned) and _version(pinned) is not None and _version(pinned) != _version(running)
    base = dict(running=running, pinned=pinned, channel=name, pinned_differs=pinned_differs)
    if not check_enabled(config):
        return UpdateState(**base, latest=None, available=False, direct=True, release=None, checked_at=None, error=None, apply="off")
    feed = load_feed(refresh=refresh)
    mode = "updater" if updater_alive() else "manual"
    latest = feed.latest(name)
    current = _version(running)
    if latest is None or current is None:
        error = feed.error or ("no release listed" if latest is None else f"running version {running!r} is not a version")
        return UpdateState(**base, latest=latest, available=False, direct=True, release=feed.releases.get(latest) if latest else None,
                           checked_at=feed.checked_at or None, error=error, apply=mode)
    entry = feed.releases[latest]
    newest = Version(latest)
    available = newest > current
    floor = _version(entry.get("min_from"))
    direct = floor is None or current >= floor
    newer = sorted((e for v, e in feed.releases.items() if Version(v) > current and (name == "prerelease" or not Version(v).is_prerelease)),
                   key=lambda e: Version(e["version"]), reverse=True)
    notes: Dict[str, Any] = {}
    if available:
        notes = _notes_for(current, newest, prereleases=name == "prerelease", refresh=refresh)
    return UpdateState(
        **base, latest=latest, available=available, direct=direct, release=entry, checked_at=feed.checked_at or None,
        error=feed.error, apply=mode, via=None if direct or not available else _step_through(feed, current, newest), releases=newer, **notes,
    )


def _notes_for(running: Version, target: Version, *, prereleases: bool, refresh: bool) -> Dict[str, Any]:
    """``release_notes``, ``notes_source`` and ``notes_error`` for the state; never raises."""
    try:
        notes = load_notes(refresh=refresh)
        if notes is None:
            return {}
        return {"release_notes": notes_between(notes, running, target, prereleases=prereleases), "notes_source": notes.source, "notes_error": notes.error}
    except Exception as exc:  # noqa: BLE001 - the notes are optional
        logger.warning("release notes skipped (%s)", exc)
        return {"notes_error": str(exc)}


# ── the files the updater reads and writes ──────────────────────────────────

def _need_dir() -> Path:
    path = extensions.directory()
    if path is None:
        raise UpdatesError("updates_unmanaged", "This Compass has no extensions volume to hand an update request to; update it the way it was deployed.")
    return path


def request() -> Optional[Dict[str, Any]]:
    path = extensions.directory()
    record = extensions.read_json(path / "update.json", None) if path is not None else None
    return record if isinstance(record, dict) and record.get("target") else None


def pending() -> Optional[Dict[str, Any]]:
    """The update the updater is in the middle of (between ``compose up`` and the health check)."""
    path = extensions.directory()
    record = extensions.read_json(path / "update.pending", None) if path is not None else None
    return record if isinstance(record, dict) else None


def result() -> Optional[Dict[str, Any]]:
    path = extensions.directory()
    record = extensions.read_json(path / "update.result.json", None) if path is not None else None
    return record if isinstance(record, dict) and record.get("status") else None


def rollback_offer() -> Optional[Dict[str, Any]]:
    """``{"to", "until"}`` while the previous version is still offered (seven days)."""
    path = extensions.directory()
    record = extensions.read_json(path / "rollback.json", None) if path is not None else None
    try:
        if isinstance(record, dict) and record.get("to") and float(record.get("until") or 0) > time.time():
            return record
    except (TypeError, ValueError):
        pass
    return None


def status_file() -> Optional[Dict[str, Any]]:
    path = extensions.directory()
    record = extensions.read_json(path / "status.json", None) if path is not None else None
    return record if isinstance(record, dict) else None


def write_status(active_jobs: int, *, by: str) -> Optional[Dict[str, Any]]:
    """``status.json``: the updater refuses to start while ``active_jobs`` is above zero."""
    path = extensions.directory()
    if path is None:
        return None
    record = {"version": running_version(), "active_jobs": int(active_jobs), "at": time.time(), "by": by}
    extensions.write_json(path / "status.json", record)
    return record


def count_active_jobs(config: Dict[str, Any], workspace_id: Optional[str]) -> int:
    """Queued, running or retrying jobs in the workspace's jobs table (0 without one)."""
    from compass_core.jobs import store_for

    store = store_for(config)
    if store is None:
        return 0
    jobs = store.list_jobs(limit=200, workspace_id=workspace_id)
    return sum(1 for j in jobs if str(j.get("status") or "").lower() in ("queued", "running", "retrying"))


def manual_commands(target: Optional[str]) -> List[str]:
    """What to run by hand, where there is no updater."""
    version = target or running_version()
    return [
        f"sed -i.bak 's/^COMPASS_VERSION=.*/COMPASS_VERSION={version}/' .env   # or edit the line",
        "docker compose pull && docker compose up -d",
    ]


def incompatible_captains(target: str) -> Dict[str, str]:
    """Installed captains whose ``requires_core`` excludes ``target``: name -> the range."""
    from compass_core import captains

    out: Dict[str, str] = {}
    for manifest in captains.discover().values():
        if manifest.is_core:
            continue
        try:
            spec = SpecifierSet(manifest.requires_core or "")
        except InvalidSpecifier:
            continue
        if not spec.contains(target, prereleases=True):
            out[manifest.name] = manifest.requires_core
    return out


def _write_request(path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    with extensions.lock(path):
        extensions.write_json(path / "update.json", record)
    return record


def request_apply(
    target: Optional[str] = None,
    *,
    force: bool = False,
    by: Optional[str] = None,
    mode: str = "manual",
    wait_minutes: int = DEFAULT_WAIT_MINUTES,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write ``update.json`` for the updater. ``target`` defaults to the channel's latest.

    Refused, unless ``force``: a version below the running one (a downgrade; data
    written by the newer version may not read back), one the running version
    cannot reach directly (``min_from``), and one an installed captain's
    ``requires_core`` excludes. Always refused: a version the feed does not list,
    no updater, a request already waiting."""
    state = check(config=config)
    if state.apply == "off":
        raise UpdatesError("updates_off", "Update checks are turned off (COMPASS_UPDATE_CHECK or the policy); turn them on to update from here.")
    path = _need_dir()
    if state.apply != "updater":
        raise UpdatesError("no_updater", "No updater is running: apply the update by hand, or start one with `docker compose --profile updater up -d`.",
                           commands=manual_commands(target or state.latest))
    feed = load_feed()
    chosen = str(Version(str(target))) if target and _version(target) else state.latest
    if not chosen or chosen not in feed.releases:
        raise UpdatesError("unknown_release", f"{target or '(latest)'} is not a release the feed lists.", status=404)
    entry = feed.releases[chosen]
    current = _version(state.running)
    wanted = Version(chosen)
    if current is not None and wanted < current and not force:
        raise UpdatesError("downgrade", f"{chosen} is older than the running {state.running}: data written since may not read back. Pass force to do it anyway.")
    floor = _version(entry.get("min_from"))
    if current is not None and floor is not None and current < floor and not force:
        via = _step_through(feed, current, wanted)
        raise UpdatesError("not_direct", f"{chosen} upgrades from {entry['min_from']} or later; update to {via or entry['min_from']} first.", via=via)
    blocked = incompatible_captains(chosen)
    if blocked and not force:
        names = ", ".join(f"{name} (needs {spec})" for name, spec in sorted(blocked.items()))
        raise UpdatesError("captains_incompatible", f"Installed captains would stop working on {chosen}: {names}. Pass force to update anyway.", captains=blocked)
    if request() is not None:
        raise UpdatesError("request_pending", f"An update to {request()['target']} is already waiting for the updater.")
    record = {
        "target": chosen, "digests": dict(entry.get("images") or {}), "requested_at": time.time(), "by": by,
        "mode": mode if mode in ("manual", "auto") else "manual", "force": bool(force), "wait_minutes": int(wait_minutes), "from": state.running,
    }
    return _write_request(path, record)


def request_rollback(*, by: Optional[str] = None) -> Dict[str, Any]:
    """Write ``update.json`` for the version ``rollback.json`` offers (its images are still local)."""
    offer = rollback_offer()
    if offer is None:
        raise UpdatesError("nothing_to_roll_back", "There is no previous version to roll back to.", status=404)
    path = _need_dir()
    if apply_mode() != "updater":
        raise UpdatesError("no_updater", "No updater is running: roll back by hand, or start one with `docker compose --profile updater up -d`.",
                           commands=manual_commands(str(offer["to"])))
    if request() is not None:
        raise UpdatesError("request_pending", f"An update to {request()['target']} is already waiting for the updater.")
    entry = load_feed().releases.get(str(offer["to"])) or {}
    record = {
        "target": str(offer["to"]), "digests": dict(entry.get("images") or {}), "requested_at": time.time(), "by": by,
        "mode": "manual", "force": True, "wait_minutes": DEFAULT_WAIT_MINUTES, "from": running_version(), "rollback": True,
    }
    return _write_request(path, record)


# ── automatic updates (the daemon) ──────────────────────────────────────────

def allowed_step(auto: str, running: str, latest: str) -> bool:
    """Whether the policy lets ``running`` move to ``latest`` on its own: ``patch`` the
    last number only; ``minor`` the middle one too (before 1.0 that is every release
    of the 0.x line; from 1.0, anything under the same major)."""
    current, newest = _version(running), _version(latest)
    if current is None or newest is None or newest <= current or newest.is_prerelease:
        return False
    if auto == "patch":
        return newest.release[:2] == current.release[:2]
    if auto == "minor":
        return newest.major == current.major
    return False


def auto_request(config: Optional[Dict[str, Any]] = None, *, now: Optional[datetime] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Ask for the update the policy allows, if any. Returns (the request, why not)."""
    auto = policy(config)["auto"]
    if auto not in ("patch", "minor"):
        return None, f"policy is {auto}"
    state = check(config=config)
    if state.apply != "updater":
        return None, f"apply is {state.apply}"
    if not state.available or not state.latest:
        return None, "nothing newer"
    if not state.direct:
        return None, f"{state.latest} is not reachable directly from {state.running}"
    if not allowed_step(auto, state.running, state.latest):
        return None, f"{state.running} to {state.latest} is more than a {auto} step"
    if not in_window(now):
        return None, f"outside the window {window_text()}"
    if request() is not None or pending() is not None:
        return None, "a request is already waiting"
    last = result() or {}
    if last.get("status") == "failed" and last.get("to") == state.latest and last.get("mode") == "auto":
        return None, f"the automatic update to {state.latest} failed and is not retried"
    if incompatible_captains(state.latest):
        return None, "an installed captain would stop working"
    try:
        return request_apply(state.latest, by="policy", mode="auto", config=config), "requested"
    except UpdatesError as exc:
        return None, str(exc)


# ── what the API and the CLI show ───────────────────────────────────────────

def status(*, refresh: bool = False, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = check(refresh=refresh, config=config)
    beat = updater_heartbeat()
    return {
        "update": state.to_dict(),
        "policy": policy(config),
        "request": request(),
        "pending": pending(),
        "result": result(),
        "rollback": rollback_offer(),
        "jobs": status_file(),
        "updater": {"alive": updater_alive(), "at": beat.get("at") if beat else None, "version": beat.get("version") if beat else None},
        "commands": manual_commands(state.latest if state.available else None),
    }
