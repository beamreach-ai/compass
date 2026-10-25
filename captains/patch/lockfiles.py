"""Lockfiles (docs/specs/patching-captain.md open decision 1, taken as "ci"): when a fix moves a
manifest that has a lockfile beside it, the pull request adds ``compass-lockfile.yml``, a
workflow that regenerates that lockfile on the branch with the ecosystem's own tool and
pushes one commit. Compass's image has none of those toolchains and must not grow them;
the repository's CI already has them. Python pins in ``requirements*.txt`` need no lockfile
and are edited in place.

The commit is made as Compass's author, so the PR watcher knows it for its own. A push made
with the default ``GITHUB_TOKEN`` does not start the repository's other workflows on that
commit; a token in ``secrets.COMPASS_LOCKFILE_TOKEN`` does, and the workflow uses it when
it is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

WORKFLOW_NAME = "compass-lockfile"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_NAME}.yml"

# manifest → the lockfiles it may have beside it, in the order they are looked for
LOCKFILES_FOR_MANIFEST: Dict[str, Sequence[str]] = {
    "composer.json": ("composer.lock",),
    "package.json": ("pnpm-lock.yaml", "yarn.lock", "package-lock.json"),
    "pyproject.toml": ("uv.lock", "poetry.lock"),
    "go.mod": ("go.sum",),
    "Cargo.toml": ("Cargo.lock",),
}

# lockfile → (the tool, the setup steps, the command that regenerates it for the moved packages)
TOOLS: Dict[str, Dict[str, Any]] = {
    "composer.lock": {"tool": "composer", "setup": ["uses: shivammathur/setup-php@v2\n        with:\n          php-version: '8.3'\n          tools: composer"],
                      "command": "composer update ${{ matrix.packages }} --with-all-dependencies --no-install --no-scripts --no-interaction"},
    "package-lock.json": {"tool": "npm", "setup": ["uses: actions/setup-node@v4\n        with:\n          node-version: '20'"],
                          "command": "npm install --package-lock-only --ignore-scripts --no-audit --no-fund"},
    "yarn.lock": {"tool": "yarn", "setup": ["uses: actions/setup-node@v4\n        with:\n          node-version: '20'", "run: corepack enable"],
                  "command": "if [ -f .yarnrc.yml ]; then yarn install --mode update-lockfile; else yarn install --ignore-scripts --non-interactive; fi"},
    "pnpm-lock.yaml": {"tool": "pnpm", "setup": ["uses: pnpm/action-setup@v4", "uses: actions/setup-node@v4\n        with:\n          node-version: '20'"],
                       "command": "pnpm install --lockfile-only --ignore-scripts"},
    "uv.lock": {"tool": "uv", "setup": ["uses: astral-sh/setup-uv@v3"], "command": "uv lock"},
    "poetry.lock": {"tool": "poetry", "setup": ["uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'", "run: pipx install poetry"],
                    "command": "poetry lock --no-update"},
    "go.sum": {"tool": "go", "setup": ["uses: actions/setup-go@v5\n        with:\n          go-version-file: ${{ matrix.dir }}/go.mod"], "command": "go mod tidy"},
    "Cargo.lock": {"tool": "cargo", "setup": ["uses: dtolnay/rust-toolchain@stable"],
                   "command": "for p in ${{ matrix.packages }}; do cargo update -p \"${p%%@*}\" --precise \"${p##*@}\"; done"},
}


@dataclass(frozen=True)
class LockEntry:
    manifest: str
    lockfile: str
    directory: str
    packages: tuple   # ("laravel/framework@11.2.1", ...) for the tools that take them

    @property
    def tool(self) -> str:
        return TOOLS[self.lockfile.rsplit("/", 1)[-1]]["tool"]


def mode(config: Mapping[str, Any]) -> str:
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    value = str(section.get("lockfiles") or "ci").lower()
    return value if value in ("ci", "manifest-only") else "ci"


def plan(manifest_edits: Mapping[str, Iterable[Mapping[str, Any]]], exists: Callable[[str], bool], *, known_lockfiles: Iterable[str] = ()) -> List[LockEntry]:
    """``manifest_edits`` maps an edited manifest path to its findings; ``exists`` says whether a path is
    in the repository; ``known_lockfiles`` are paths the map already saw. One entry per manifest with a
    lockfile beside it."""
    known = set(known_lockfiles)
    out: List[LockEntry] = []
    for manifest, findings in manifest_edits.items():
        name = manifest.rsplit("/", 1)[-1]
        directory = manifest[: -len(name)].rstrip("/") or "."
        candidates = LOCKFILES_FOR_MANIFEST.get(name, ())
        lockfile = None
        for candidate in candidates:
            path = f"{directory}/{candidate}" if directory != "." else candidate
            if path in known or exists(path):
                lockfile = path
                break
        if lockfile is None:
            continue
        packages = tuple(sorted({f"{f.get('package')}@{f.get('fixed_version')}" for f in findings if f.get("package") and f.get("fixed_version")}))
        out.append(LockEntry(manifest=manifest, lockfile=lockfile, directory=directory, packages=packages))
    return out


def workflow(entries: Sequence[LockEntry], *, author_email: str, author_name: str = "compass") -> str:
    """The workflow: one job per lockfile, the tool set up, the lockfile regenerated in its directory and
    pushed as one commit by Compass's author."""
    jobs = []
    for entry in sorted(entries, key=lambda e: e.lockfile):
        tool = TOOLS[entry.lockfile.rsplit("/", 1)[-1]]
        job_id = "lock_" + "".join(ch if ch.isalnum() else "_" for ch in entry.lockfile).strip("_").lower()
        setup = "\n".join(f"      - {step}" for step in tool["setup"])
        jobs.append(f"""  {job_id}:
    name: regenerate {entry.lockfile}
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - {{ dir: "{entry.directory}", lockfile: "{entry.lockfile}", packages: "{' '.join(entry.packages)}" }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ github.head_ref }}}}
          token: ${{{{ secrets.COMPASS_LOCKFILE_TOKEN || secrets.GITHUB_TOKEN }}}}
{setup}
      - name: regenerate the lockfile
        working-directory: ${{{{ matrix.dir }}}}
        run: {tool['command']}
      - name: commit it to this branch
        run: |
          git config user.name "{author_name}"
          git config user.email "{author_email}"
          git add "${{{{ matrix.lockfile }}}}"
          git diff --cached --quiet && echo "the lockfile was already current" && exit 0
          git commit -m "compass: regenerate ${{{{ matrix.lockfile }}}} for the security patch"
          git push""")
    manifests = sorted({e.manifest for e in entries})
    return f"""# Added by Compass's patch captain: the manifest moved, the lockfile follows on this branch.
# Set secrets.COMPASS_LOCKFILE_TOKEN (a token allowed to push) so the commit starts your other workflows;
# a push with GITHUB_TOKEN does not. Keep or remove this file once the pull request merged.
name: {WORKFLOW_NAME}
on:
  pull_request:
    branches: ["**"]
    paths:
{chr(10).join(f'      - "{m}"' for m in manifests)}
permissions:
  contents: write
  pull-requests: read
concurrency:
  group: {WORKFLOW_NAME}-${{{{ github.head_ref }}}}
  cancel-in-progress: true
jobs:
{chr(10).join(jobs)}
"""


def author_email(config: Mapping[str, Any]) -> str:
    try:
        from captains.prs.sync import compass_identity

        emails = sorted(compass_identity(dict(config)).get("emails") or [])
        if emails:
            return emails[0]
    except Exception:  # noqa: BLE001
        pass
    return "compass@beamreach.ai"


def note(entry: Optional[LockEntry], lock_mode: str) -> str:
    if lock_mode == "manifest-only":
        return "the lockfile needs regenerating (patch.lockfiles is manifest-only)"
    if entry is None:
        return "no lockfile beside the manifest; nothing to regenerate"
    return f"{entry.lockfile} is regenerated on this branch by {WORKFLOW_PATH} ({entry.tool})"
