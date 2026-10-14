"""Git working copy for the import pipeline.

The previous implementation wrote files through the GitHub Contents API, which
replaces a file's entire contents by design. That is the direct cause of the
worst bug this pipeline has had: an import into `environments/demo` overwrote
the environment's hand-written root module. There is no way to express "add to
this file" through that API without first fetching, merging and re-uploading.

A real checkout removes the whole class of problem, and is also a hard
requirement for everything else — terraform cannot init, plan, or resolve module
outputs against a repository it cannot see.

Credentials are injected per-invocation and never written to disk: the remote
keeps its plain URL and the token is supplied through an `http.extraheader`
config on each call, so it does not land in `.git/config` or in the reflog.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("iac-repo-checkout")

DEFAULT_TIMEOUT_S = 300
_BOT_NAME = "beamreach-compass[bot]"
_BOT_EMAIL = "beamreach-compass[bot]@users.noreply.github.com"


class CheckoutError(RuntimeError):
    pass


@dataclass
class Checkout:
    path: str
    repo: str
    base_branch: str

    @property
    def root(self) -> Path:
        return Path(self.path)


class RepoCheckout:
    def __init__(self, token: str, timeout_s: int = DEFAULT_TIMEOUT_S):
        if not token:
            raise CheckoutError("a GitHub token is required to clone")
        self._token = token
        self.timeout_s = timeout_s
        self._workdir: Optional[str] = None

    # ------------------------------------------------------------------ plumbing

    def _auth_args(self) -> List[str]:
        """Per-invocation credentials.

        `x-access-token:<token>` base64-encoded as a Basic header, passed with
        -c so it applies to this command only. Keeps the token out of the
        remote URL, which git would otherwise persist in .git/config.
        """
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return ["-c", f"http.extraheader=Authorization: Basic {basic}"]

    def _git(self, args: Sequence[str], cwd: Optional[str] = None, authed: bool = False) -> str:
        cmd = ["git"]
        if authed:
            cmd += self._auth_args()
        cmd += list(args)
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        if proc.returncode != 0:
            # Never echo the command: it may carry the auth header.
            raise CheckoutError(f"git {args[0]} failed ({proc.returncode}): {(proc.stderr or '').strip()[:800]}")
        return proc.stdout

    # -------------------------------------------------------------------- api

    def clone(self, repo: str, base_branch: str) -> Checkout:
        """Shallow clone of a single branch — history is never needed here."""
        self._workdir = tempfile.mkdtemp(prefix="beamreach-iac-")
        dest = str(Path(self._workdir) / "repo")
        url = f"https://github.com/{repo}.git"
        self._git(
            ["clone", "--depth", "1", "--branch", base_branch, "--single-branch", url, dest],
            authed=True,
        )
        logger.info("cloned %s@%s", repo, base_branch)
        return Checkout(path=dest, repo=repo, base_branch=base_branch)

    def write_files(self, checkout: Checkout, files: Dict[str, str]) -> List[str]:
        """Write generated files into the working copy.

        Refuses to touch a file that already exists. Generated names are
        beamreach-specific, so a collision means either a previous import left
        artefacts on this branch or a name has been chosen that clashes with the
        repository's own code — both worth failing on rather than overwriting.
        """
        written: List[str] = []
        for rel_path, content in files.items():
            target = checkout.root / rel_path
            if target.exists():
                raise CheckoutError(
                    f"refusing to overwrite existing file {rel_path}; "
                    "generated files must be new"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel_path)
        return written

    def create_branch(self, checkout: Checkout, branch: str) -> None:
        self._git(["checkout", "-B", branch], cwd=checkout.path)

    def commit(self, checkout: Checkout, message: str, paths: Sequence[str]) -> bool:
        """Stage and commit. Returns False when there is nothing to commit."""
        self._git(["add", "--", *paths], cwd=checkout.path)
        status = self._git(["status", "--porcelain"], cwd=checkout.path)
        if not status.strip():
            logger.info("nothing to commit")
            return False
        self._git(
            [
                "-c", f"user.name={_BOT_NAME}",
                "-c", f"user.email={_BOT_EMAIL}",
                "commit", "-m", message,
            ],
            cwd=checkout.path,
        )
        return True

    def push(self, checkout: Checkout, branch: str, force: bool = False) -> None:
        args = ["push", "--set-upstream", "origin", branch]
        if force:
            # `--force-with-lease` cannot be used here. The clone is shallow and
            # single-branch, so there is no remote-tracking ref for the import
            # branch, and git rejects the push with "stale info" rather than
            # comparing anything — the lease has nothing to lease against.
            #
            # A plain force is the correct semantic regardless: the branch name
            # is content-addressed from the resource set and lives under the
            # beamreach/ prefix, so this pipeline is its only author and a
            # re-import is meant to replace it wholesale.
            args.insert(1, "--force")
        self._git(args, cwd=checkout.path, authed=True)
        logger.info("pushed %s", branch)

    def cleanup(self) -> None:
        if self._workdir and Path(self._workdir).exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def __enter__(self) -> "RepoCheckout":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
