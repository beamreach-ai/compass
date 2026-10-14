import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


def _run_git_command(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()
    except Exception as exc:  # pragma: no cover - best effort helper
        logger.debug(f"[git_metadata] git {' '.join(args)} failed in {cwd}: {exc}")
        return None


def _normalize_remote(remote: str) -> Dict[str, str]:
    remote = remote.strip()
    if not remote:
        return {}

    # Strip credentials if present (e.g. https://token@github.com/org/repo.git)
    if "://" in remote:
        scheme, rest = remote.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        remote = f"{scheme}://{rest}"

    slug = ""
    host = ""

    if remote.startswith("git@"):
        # git@github.com:owner/repo.git
        _, rest = remote.split("@", 1)
        if ":" in rest:
            host, path = rest.split(":", 1)
        else:
            host, path = rest.split("/", 1)
        slug = path
    elif remote.startswith("https://") or remote.startswith("http://"):
        scheme, rest = remote.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
        else:
            host, path = rest, ""
        slug = path
        remote = f"{scheme}://{host}/{path}"
    else:
        # Unsupported scheme
        return {}

    slug = slug.strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    if not slug:
        return {}

    host = host.strip("/")
    base = f"https://{host}" if host else "https://github.com"

    normalized = {
        "repo": slug,
        "repo_full_name": slug,
        "repo_clone_url": f"{base}/{slug}.git",
        "repo_html_url": f"{base}/{slug}",
        "git_remote": remote,
    }
    return normalized


@lru_cache(maxsize=128)
def probe_git_metadata(path: str) -> Dict[str, str]:
    """
    Inspect ``path`` (file or directory) and, when inside a Git working tree,
    return sanitized metadata including repository slug, remote URLs, and branch.
    Returns {} when the location is not under Git control.
    """
    if not path:
        return {}
    try:
        resolved = Path(path).resolve()
    except Exception:
        resolved = Path(path)
    if not resolved.exists():
        return {}

    if resolved.is_file():
        resolved = resolved.parent

    toplevel = _run_git_command(["rev-parse", "--show-toplevel"], str(resolved))
    if not toplevel:
        return {}

    remote = _run_git_command(["remote", "get-url", "origin"], toplevel)
    branch = _run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], toplevel)

    meta: Dict[str, str] = {
        "git_toplevel": toplevel,
    }
    if branch and branch != "HEAD":
        meta["git_branch"] = branch
    if remote:
        meta.update(_normalize_remote(remote))
    return meta


def sanitize_repo_slug(slug: str) -> str:
    """
    Convert a repo slug (e.g. owner/repo) into a filesystem-friendly token.
    """
    if not slug:
        return ""
    slug = slug.strip()
    slug = slug.replace("/", "__")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", slug)
