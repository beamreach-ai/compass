"""Which end-to-end tests a change needs (docs/e2e.md, "In CI").

    e2e_plan.py --full                       every shard, and the compose update test
    e2e_plan.py --changed changed-files.txt   what the listed paths need

Prints JSON and, under GitHub Actions, writes the same as step outputs:

    run             "true" when any test runs
    matrix          {"include": [{"shard": "A", "name": "...", "files": "e2e/test_00_install.py ..."}]}
    compose_update  "true" when the slow real-update test (UPD-2, tests/e2e/update_compose.sh) runs
    reason          one line on why

The rules, most specific first. A change that matches none of them runs everything:
selecting tests is a way to skip what cannot matter, never a guess at what might.

- Nothing that runs in the stack (docs, the changelog, the CLI, unit tests, the
  editor config): no end-to-end test.
- Only one captain's own folder, a captain with its own tests: that captain's test
  files, plus the install and shell smoke tests.
- Only end-to-end test files: those files (and the ones they build on).
- Anything else (compass_core, compass_api, the dashboard, docker/, the compose file,
  the harness, the fakes, the mapper every other captain reads): everything.

The compose update test runs only when what an update touches changed: the images,
the compose file, the updater, the entrypoint, the update and extension code.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from typing import Dict, Iterable, List, Optional

# The four shards: each a set of files that run on one runner with one stack. Files that
# build on another's state (the PR watcher on the pull requests code-as-cloud opens) share one.
SHARDS: Dict[str, Dict[str, object]] = {
    "A": {"name": "install, sign-in, shell, settings",
          "files": ["test_00_install.py", "test_01_auth.py", "test_02_shell.py", "test_03_settings.py"]},
    "B": {"name": "captains, the map", "files": ["test_04_captains.py", "test_05_map.py"]},
    "C": {"name": "decoys, code-as-cloud, pull requests", "files": ["test_06_decoys.py", "test_07_iac.py", "test_08_prs.py"]},
    "D": {"name": "jobs, updates, release", "files": ["test_09_jobs.py", "test_10_updates.py", "test_11_release.py"]},
}
SMOKE = ["test_00_install.py", "test_02_shell.py"]
NEEDS = {"test_08_prs.py": ["test_07_iac.py"]}  # PRS tests watch the pull request IAC-2 opens

# Paths that do not change what runs in the stack.
OUTSIDE = ["docs/*", "*.md", "cli/*", "tests/*", ".claude/*", ".vscode/*", "LICENSE", "CODEOWNERS", "SECURITY.md",
           ".github/dependabot.yml", ".github/ISSUE_TEMPLATE/*"]
# ...except these, which the suite does read or run.
INSIDE_ANYWAY = ["tests/e2e/*", "README.md", "docs/quickstart.md", "CHANGELOG.md"]
# A captain's own folder and the end-to-end files that test it. The mapper is not here:
# every other captain reads the map, so a mapper change runs everything.
CAPTAINS = {
    "captains/honeypot/*": ["test_06_decoys.py"],
    "captains/iac/*": ["test_07_iac.py", "test_08_prs.py"],
    "captains/prs/*": ["test_07_iac.py", "test_08_prs.py"],
    "captains/librarian/*": [],
    "examples/captain-hello/*": ["test_04_captains.py"],
}
COMPOSE_UPDATE = ["docker/*", "docker-compose.yml", ".env.example", "nginx.conf", "compass_core/updates.py",
                  "compass_core/extensions.py", "compass_core/migrations/*", "compass_core/serve.py", "compass_api/routes/updates.py",
                  "tests/e2e/update_compose.sh", "pyproject.toml", "dashboard/Dockerfile.dashboard"]


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _with_needs(files: Iterable[str]) -> List[str]:
    wanted = set(files)
    for name in list(wanted):
        wanted.update(NEEDS.get(name, []))
    return sorted(wanted)


def _matrix(files: Optional[List[str]]) -> Dict[str, List[Dict[str, str]]]:
    """Shards holding the files (all of them when None), in shard order."""
    include = []
    for shard, spec in SHARDS.items():
        chosen = [f for f in spec["files"] if files is None or f in files]  # type: ignore[union-attr]
        if chosen:
            include.append({"shard": shard, "name": str(spec["name"]), "files": " ".join(f"e2e/{f}" for f in chosen)})
    return {"include": include}


def plan(changed: Optional[List[str]]) -> Dict[str, object]:
    """``changed`` None means everything (nightly, a release, by hand)."""
    if changed is None:
        return {"run": True, "matrix": _matrix(None), "compose_update": True, "reason": "the full suite"}
    changed = [path.strip() for path in changed if path.strip()]
    compose_update = any(_matches(path, COMPOSE_UPDATE) for path in changed)
    relevant = [path for path in changed if not _matches(path, OUTSIDE) or _matches(path, INSIDE_ANYWAY)]
    if not relevant:
        return {"run": False, "matrix": {"include": []}, "compose_update": False,
                "reason": "nothing that runs in the stack changed"}

    selected: List[str] = []
    for path in relevant:
        captain = next((files for pattern, files in CAPTAINS.items() if fnmatch.fnmatch(path, pattern)), None)
        if captain is not None:
            selected += captain + SMOKE
            continue
        if fnmatch.fnmatch(path, "e2e/test_*.py") and "/" not in path[len("e2e/"):]:
            selected += [path[len("e2e/"):]]
            continue
        return {"run": True, "matrix": _matrix(None), "compose_update": compose_update,
                "reason": f"{path} is shared by every area"}
    files = _with_needs(selected)
    return {"run": True, "matrix": _matrix(files), "compose_update": compose_update and "test_10_updates.py" in files,
            "reason": f"only {', '.join(sorted({p.split('/')[0] + '/' + p.split('/')[1] if '/' in p else p for p in relevant}))} changed"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--changed", help="a file listing the changed paths, one per line")
    args = parser.parse_args(argv)
    if not args.full and not args.changed:
        parser.error("--full or --changed")
    changed = None if args.full else open(args.changed, encoding="utf-8").read().splitlines()
    result = plan(changed)
    print(json.dumps(result, indent=2))
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if result['run'] else 'false'}\n")
            handle.write(f"matrix={json.dumps(result['matrix'])}\n")
            handle.write(f"compose_update={'true' if result['compose_update'] else 'false'}\n")
            handle.write(f"reason={result['reason']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
