"""CI guard for the honeypot vocabulary (docs/honeypot_replay_spec.md section 0).

"canary" is reserved for A/B canary deployments and must not appear anywhere in
the honeypot feature. The planted item is a *honeytoken*; "asset" and "lure"
are retired names for it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]

GUARDED = [
    "captains/honeypot",
    "compass_api/routes/honeypots.py",
    "compass_core/keys.py",
    "tests/captains/honeypot",
    "dashboard/app/components",
]
_SKIP_DIRS = {"node_modules", "__pycache__", ".next", ".git"}
_SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".svg", ".ico", ".woff", ".woff2"}
_THIS_FILE = pathlib.Path(__file__).resolve()
_WORD = re.compile("canary", re.IGNORECASE)


def _files(rel: str):
    path = REPO / rel
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for p in path.rglob("*"):
        if p.is_file() and not (set(p.parts) & _SKIP_DIRS) and p.suffix not in _SKIP_SUFFIXES:
            yield p


def _hits(rel: str):
    out = []
    for f in _files(rel):
        if f.resolve() == _THIS_FILE:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _WORD.search(line):
                out.append(f"{f.relative_to(REPO)}:{n}: {line.strip()[:140]}")
    return out


@pytest.mark.parametrize("rel", GUARDED)
def test_no_canary_in_honeypot_feature(rel):
    hits = _hits(rel)
    assert not hits, "retired word found (use 'honeytoken'):\n" + "\n".join(hits)


def test_retired_names_are_gone_from_python_api_surface():
    from compass_api.routes import honeypots

    names = set(dir(honeypots))
    assert not {n for n in names if "asset" in n.lower() or "lure" in n.lower()}
    assert "HONEYTOKEN_CLASSES" in names
