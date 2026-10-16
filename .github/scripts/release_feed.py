"""What the release workflow runs around a tag (docs/open_updates_spec.md §2).

    release_feed.py notes 0.5.0                     the CHANGELOG.md section for the version, or exit 1
    release_feed.py feed 0.5.0 --feed releases.json --notes-file notes.md --notes-url … \
        --image compass=sha256:… --image compass-dashboard=sha256:… [--tag-message …] [--min-from 0.4.0]
                                                    add the release to the feed and recompute the channels
    release_feed.py bump-defaults 0.5.0             COMPASS_VERSION defaults in docker-compose.yml and .env.example

A changelog section is ``## <version>`` (anything may follow on the line, a date
say) down to the next ``## ``. Its first paragraph is the release's summary; the
bullets under ``### After the update`` are the notes the page shows next to the
Update button. Stdlib plus ``packaging``; no Compass import, so it runs before
anything is built.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from packaging.version import InvalidVersion, Version

SCHEMA = 1
_SECTION_RE = re.compile(r"^## +v?(\S+)(.*)$")
_AFTER_RE = re.compile(r"^### +after the update\s*$", re.IGNORECASE)


def _version(text: Any) -> Optional[Version]:
    try:
        return Version(str(text))
    except InvalidVersion:
        return None


# ── the changelog ───────────────────────────────────────────────────────────

def changelog_section(text: str, version: str) -> Optional[str]:
    """The body of ``## <version>``, without the heading; None when there is no such section."""
    wanted = _version(version)
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match and _version(match.group(1)) == wanted and wanted is not None:
            start = index + 1
            break
    if start is None:
        return None
    body: List[str] = []
    for line in lines[start:]:
        if _SECTION_RE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip("\n")


def summary_and_notes(section: str, *, tag_message: Optional[str] = None, version: str = "") -> Dict[str, Any]:
    """The first paragraph (else the tag's subject without its ``Compass x.y.z:`` prefix)
    and the bullets under ``### After the update``."""
    paragraphs: List[str] = []
    notes: List[str] = []
    current: List[str] = []
    in_after = False
    for line in section.splitlines() + [""]:
        if _AFTER_RE.match(line):
            in_after = True
            continue
        if line.startswith("#"):
            in_after = False
        if in_after:
            if line.lstrip().startswith(("-", "*")):
                notes.append(line.lstrip()[1:].strip())
            continue
        if line.strip() and not line.lstrip().startswith(("-", "*", "#")):
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    summary = paragraphs[0] if paragraphs else ""
    if not summary and tag_message:
        summary = re.sub(rf"^\s*compass\s+v?{re.escape(version)}\s*:\s*", "", tag_message.strip().splitlines()[0], flags=re.IGNORECASE)
    return {"summary": summary, "notes": notes}


# ── the feed ────────────────────────────────────────────────────────────────

def default_min_from(version: str) -> str:
    """The previous minor: what a release upgrades from directly unless it says otherwise."""
    v = Version(version)
    major, minor = v.major, v.minor
    if minor > 0:
        return f"{major}.{minor - 1}.0"
    return f"{major - 1}.0.0" if major > 0 else "0.0.0"


def channels(releases: List[Dict[str, Any]]) -> Dict[str, str]:
    versions = [Version(r["version"]) for r in releases]
    stable = [v for v in versions if not v.is_prerelease]
    out: Dict[str, str] = {}
    if stable:
        out["stable"] = str(max(stable))
    if versions:
        out["prerelease"] = str(max(versions))
    return out


def add_release(feed: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> Dict[str, Any]:
    """The feed with ``entry`` in place of any entry of the same version, newest first."""
    releases = [r for r in ((feed or {}).get("releases") or []) if _version(r.get("version")) and Version(r["version"]) != Version(entry["version"])]
    releases.append(entry)
    releases.sort(key=lambda r: Version(r["version"]), reverse=True)
    return {"schema": SCHEMA, "channels": channels(releases), "releases": releases}


def build_entry(
    version: str,
    *,
    section: str,
    images: Dict[str, str],
    notes_url: str,
    published_at: Optional[str] = None,
    tag_message: Optional[str] = None,
    min_from: Optional[str] = None,
) -> Dict[str, Any]:
    text = summary_and_notes(section, tag_message=tag_message, version=version)
    return {
        "version": str(Version(version)),
        "published_at": published_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes_url": notes_url,
        "summary": text["summary"],
        "images": dict(images),
        "min_from": min_from or default_min_from(version),
        "requires_restart": ["api", "compass", "dashboard"],
        "notes": text["notes"],
    }


# ── the defaults ────────────────────────────────────────────────────────────

def bump_defaults(compose_text: str, env_text: str, version: str) -> tuple:
    compose = re.sub(r"COMPASS_VERSION:-[0-9A-Za-z.]+", f"COMPASS_VERSION:-{version}", compose_text)
    env = re.sub(r"(?m)^COMPASS_VERSION=.*$", f"COMPASS_VERSION={version}", env_text)
    return compose, env


# ── the command ─────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    notes = sub.add_parser("notes")
    notes.add_argument("version")
    notes.add_argument("--changelog", default="CHANGELOG.md")
    feed = sub.add_parser("feed")
    feed.add_argument("version")
    feed.add_argument("--feed", required=True, help="releases.json to update (created when missing)")
    feed.add_argument("--notes-file", required=True)
    feed.add_argument("--notes-url", required=True)
    feed.add_argument("--image", action="append", default=[], metavar="NAME=DIGEST")
    feed.add_argument("--tag-message", default=None)
    feed.add_argument("--published-at", default=None)
    feed.add_argument("--min-from", default=None)
    bump = sub.add_parser("bump-defaults")
    bump.add_argument("version")
    bump.add_argument("--compose", default="docker-compose.yml")
    bump.add_argument("--env", default=".env.example")
    args = parser.parse_args(argv)

    if _version(args.version) is None:
        print(f"{args.version!r} is not a version", file=sys.stderr)
        return 2
    if args.verb == "notes":
        section = changelog_section(Path(args.changelog).read_text(encoding="utf-8"), args.version)
        if section is None:
            print(f"{args.changelog} has no '## {args.version}' section: a changelog section is part of a release", file=sys.stderr)
            return 1
        print(section)
        return 0
    if args.verb == "feed":
        path = Path(args.feed)
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        images = dict(item.split("=", 1) for item in args.image)
        entry = build_entry(
            args.version, section=Path(args.notes_file).read_text(encoding="utf-8"), images=images, notes_url=args.notes_url,
            published_at=args.published_at, tag_message=args.tag_message, min_from=args.min_from,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(add_release(current, entry), indent=2) + "\n", encoding="utf-8")
        print(json.dumps(entry, indent=2))
        return 0
    compose_path, env_path = Path(args.compose), Path(args.env)
    compose, env = bump_defaults(compose_path.read_text(encoding="utf-8"), env_path.read_text(encoding="utf-8"), args.version)
    compose_path.write_text(compose, encoding="utf-8")
    env_path.write_text(env, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
