"""The fixes (docs/specs/patching-captain.md §3.5): one deterministic edit per finding, a version
string replaced by the one the advisory names, in the file the mapper read it from.

Strategies, by what the finding's software is:

- a docker-library base image: the ``FROM`` line's tag at the fixed upstream version
  (``php:8.3.30-fpm`` → ``php:8.3.36-fpm``); a digest-pinned ``FROM`` gets the new tag's digest
  when the registry answers, else loses the pin and the body says so;
- an apt or apk package in a ``RUN`` line: pinned to the fixed distro version (``php8.3-fpm`` →
  ``php8.3-fpm=8.3.6-0ubuntu0.24.04.13``), or its pin replaced;
- a Bref layer ARN in ``serverless.yml``, a SAM template or Terraform: the layer version;
- a direct dependency in a manifest (composer.json, package.json, requirements.txt,
  pyproject.toml, go.mod, Cargo.toml): the lowest fixed version; the lockfile is regenerated
  by the repository's CI (open decision 1).

The model writes nothing here. Anything a strategy cannot express as one such edit returns
None, and the finding says why it waits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from captains.patch import versions as _versions

LAYER_ARN_RE = re.compile(r"(arn:aws:lambda:[a-z0-9-]+:\d{12}:layer:)([A-Za-z0-9_-]+):(\d+)")


@dataclass
class Edit:
    path: str
    before: str
    after: str
    description: str                       # one line for the body: "FROM php:8.3.30-fpm → php:8.3.36-fpm"
    strategy: str
    notes: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.before != self.after


LOCKFILE_MANIFEST = {"composer.lock": "composer.json", "package-lock.json": "package.json", "yarn.lock": "package.json", "pnpm-lock.yaml": "package.json",
                     "uv.lock": "pyproject.toml", "poetry.lock": "pyproject.toml", "go.sum": "go.mod", "Cargo.lock": "Cargo.toml"}


def target_path(finding: Mapping[str, Any]) -> str:
    """The file the edit goes to: the finding's own, or the manifest beside the lockfile it was read from."""
    path = str(finding.get("path") or "")
    name = path.rsplit("/", 1)[-1]
    if name in LOCKFILE_MANIFEST:
        return path[: -len(name)] + LOCKFILE_MANIFEST[name]
    return path


def strategy_for(finding: Mapping[str, Any]) -> Optional[str]:
    """Which strategy fixes this finding, or None when none does yet."""
    if not finding.get("fixed_version") or finding.get("blockers"):
        return None
    relation = str(finding.get("relation") or "")
    purl_type = str(finding.get("purl_type") or "")
    path = str(finding.get("path") or "")
    name = path.rsplit("/", 1)[-1].lower()
    if relation == "base" and purl_type == "docker":
        return "dockerfile_from"
    if relation == "package" and purl_type in ("deb", "apk") and (name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile")):
        return "dockerfile_package"
    if relation == "layer":
        return "layer"
    if relation == "direct" and purl_type in ("composer", "npm", "pypi", "golang", "cargo"):
        return "manifest"
    return None


def apply(finding: Mapping[str, Any], text: str, *, digest_for_tag: Any = None) -> Optional[Edit]:
    """The edit for ``finding`` in the file's current ``text``; None when the strategy does not apply
    or the text no longer holds what the finding saw."""
    strategy = strategy_for(finding)
    if strategy is None:
        return None
    path = target_path(finding)
    if strategy == "dockerfile_from":
        return _dockerfile_from(path, text, finding, digest_for_tag)
    if strategy == "dockerfile_package":
        return _dockerfile_package(path, text, finding)
    if strategy == "layer":
        return _layer(path, text, finding)
    if strategy == "manifest":
        return _manifest(path, text, finding)
    return None


# ── Dockerfiles ───────────────────────────────────────────────────────────────

def _dockerfile_from(path: str, text: str, finding: Mapping[str, Any], digest_for_tag: Any) -> Optional[Edit]:
    image = str(finding.get("package") or "")                 # "php"
    current_tag = _purl_version(str(finding.get("purl") or ""))  # "8.3.30-fpm"
    fixed_version = str(finding.get("fixed_version") or "")
    if not image or not current_tag or not fixed_version:
        return None
    new_tag = _versions.fixed_tag(current_tag, fixed_version)
    pattern = re.compile(r"^(?P<lead>\s*FROM\s+(?:--platform=\S+\s+)?)(?P<image>(?:[\w.-]+(?::\d+)?/)*" + re.escape(image) + r"):" + re.escape(current_tag)
                         + r"(?P<digest>@sha256:[0-9a-f]{64})?(?P<tail>(?:\s+AS\s+\S+)?\s*)$", re.IGNORECASE | re.MULTILINE)
    notes: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        digest = match.group("digest") or ""
        if digest:
            new_digest = None
            if digest_for_tag is not None:
                try:
                    new_digest = digest_for_tag(match.group("image"), new_tag)
                except Exception:  # noqa: BLE001 - the registry not answering is a note, not a failure
                    new_digest = None
            if new_digest:
                digest = f"@{new_digest}" if not str(new_digest).startswith("@") else str(new_digest)
            else:
                notes.append(f"the digest pin on {match.group('image')}:{current_tag} could not be re-resolved for {new_tag}; the tag alone is kept")
                digest = ""
        return f"{match.group('lead')}{match.group('image')}:{new_tag}{digest}{match.group('tail')}"

    after, count = pattern.subn(replace, text)
    if count == 0:
        return None
    return Edit(path=path, before=text, after=after, description=f"FROM {image}:{current_tag} → {image}:{new_tag}", strategy="dockerfile_from", notes=notes)


def _dockerfile_package(path: str, text: str, finding: Mapping[str, Any]) -> Optional[Edit]:
    binary = _purl_name(str(finding.get("purl") or ""))        # "php8.3-fpm", the binary the Dockerfile installs
    fixed_version = str(finding.get("fixed_version") or "")
    current = finding.get("version")
    if not binary or not fixed_version:
        return None
    sep = "=" if str(finding.get("purl_type")) == "deb" else "="
    # the package word in an install line: pinned or not, followed by a space, a continuation or the end
    pattern = re.compile(r"(?P<lead>(?:apt-get|apt|apk)\s+(?:[\w-]+\s+)*?(?:install|add)\b[^\n]*?(?<=\s))" + re.escape(binary) + r"(?:[=~<>][^\s\\]*)?(?=\s|\\|$)", re.MULTILINE)
    replaced = {"n": 0}

    def replace(match: "re.Match[str]") -> str:
        replaced["n"] += 1
        return f"{match.group('lead')}{binary}{sep}{fixed_version}"

    after = pattern.sub(replace, text)
    if replaced["n"] == 0:
        # a continuation line: the package stands alone on its line inside the RUN
        line_pattern = re.compile(r"^(?P<indent>\s+)" + re.escape(binary) + r"(?:[=~<>][^\s\\]*)?(?P<tail>\s*\\?\s*)$", re.MULTILINE)
        after, n = line_pattern.subn(lambda m: f"{m.group('indent')}{binary}{sep}{fixed_version}{m.group('tail')}", text)
        if n == 0:
            return None
    before_word = f"{binary}={current}" if current else binary
    return Edit(path=path, before=text, after=after, description=f"{before_word} → {binary}{sep}{fixed_version}", strategy="dockerfile_package")


# ── layers ────────────────────────────────────────────────────────────────────

def _layer(path: str, text: str, finding: Mapping[str, Any]) -> Optional[Edit]:
    layer = str(finding.get("package") or finding.get("purl") or "").rsplit("/", 1)[-1]   # "php-83-fpm"
    current = str(finding.get("version") or "")
    fixed_version = str(finding.get("fixed_version") or "")
    if not layer or not fixed_version.isdigit():
        return None
    pattern = re.compile(LAYER_ARN_RE.pattern.replace(r"([A-Za-z0-9_-]+)", "(" + re.escape(layer) + ")"))
    after, count = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}:{fixed_version}", text)
    if count == 0:
        return None
    return Edit(path=path, before=text, after=after, description=f"layer {layer}:{current} → {layer}:{fixed_version}", strategy="layer")


# ── manifests ─────────────────────────────────────────────────────────────────

def _manifest(path: str, text: str, finding: Mapping[str, Any]) -> Optional[Edit]:
    name = str(finding.get("package") or "")
    fixed_version = str(finding.get("fixed_version") or "")
    current = finding.get("version")
    filename = path.rsplit("/", 1)[-1]
    if not name or not fixed_version:
        return None
    after: Optional[str] = None
    if filename in ("composer.json", "package.json"):
        after = _json_dependency(text, name, fixed_version, keep_prefix=True)
    elif filename.lower().startswith("requirements") and filename.endswith(".txt"):
        after = _requirements(text, name, fixed_version)
    elif filename == "pyproject.toml":
        after = _pyproject(text, name, fixed_version)
    elif filename == "go.mod":
        after, _ = re.subn(r"(?m)^(\s*(?:require\s+)?" + re.escape(name) + r"\s+)v?\S+", lambda m: f"{m.group(1)}{fixed_version if fixed_version.startswith('v') else 'v' + fixed_version}", text)
    elif filename == "Cargo.toml":
        after = _cargo(text, name, fixed_version)
    if after is None or after == text:
        return None
    return Edit(path=path, before=text, after=after, description=f"{name} {current or ''} → {fixed_version}".replace("  ", " "), strategy="manifest")


def _json_dependency(text: str, name: str, fixed_version: str, *, keep_prefix: bool) -> Optional[str]:
    """Replace the constraint of ``name`` wherever it is declared, keeping the file's formatting:
    a caret or tilde prefix stays (``^11.2.0`` → ``^11.2.1``), an exact pin stays exact."""
    try:
        json.loads(text)
    except ValueError:
        return None
    pattern = re.compile(r'("' + re.escape(name) + r'"\s*:\s*")(?P<constraint>[^"]*)(")')

    def replace(match: "re.Match[str]") -> str:
        constraint = match.group("constraint").strip()
        prefix = ""
        if keep_prefix:
            lead = re.match(r"^([\^~]|>=)", constraint)
            prefix = lead.group(1) if lead else ""
        return f"{match.group(1)}{prefix}{fixed_version}{match.group(3)}"

    after, count = pattern.subn(replace, text)
    return after if count else None


def _requirements(text: str, name: str, fixed_version: str) -> Optional[str]:
    pattern = re.compile(r"(?mi)^(?P<name>" + re.escape(name).replace("\\-", "[-_]") + r")(?P<extras>\[[^\]]*\])?[ \t]*(?:[=~!<>]=?[ \t]*[^\s;#\\]+(?:[ \t]*,[ \t]*[=~!<>]=?[ \t]*[^\s;#\\]+)*)?(?P<rest>.*)$")
    after, count = pattern.subn(lambda m: f"{m.group('name')}{m.group('extras') or ''}=={fixed_version}{m.group('rest')}", text)
    return after if count else None


def _pyproject(text: str, name: str, fixed_version: str) -> Optional[str]:
    # PEP 621 dependency strings and poetry's table entries, whichever the file uses
    pep = re.compile(r'(?mi)(")(' + re.escape(name).replace("\\-", "[-_]") + r')(\[[^\]]*\])?\s*(?:[=~!<>]=?[^";]+)?(")')
    after, count = pep.subn(lambda m: f'{m.group(1)}{m.group(2)}{m.group(3) or ""}=={fixed_version}{m.group(4)}', text)
    if count:
        return after
    poetry = re.compile(r'(?mi)^(\s*' + re.escape(name).replace("\\-", "[-_]") + r'\s*=\s*)(?:"[^"]*"|\{[^}]*version\s*=\s*")([^"]*)(")?')
    after, count = poetry.subn(lambda m: f'{m.group(1)}"{fixed_version}"' if m.group(0).count("{") == 0 else m.group(0).replace(m.group(2), fixed_version, 1), text)
    return after if count else None


def _cargo(text: str, name: str, fixed_version: str) -> Optional[str]:
    simple = re.compile(r'(?m)^(\s*' + re.escape(name) + r'\s*=\s*")([^"]*)(")')
    after, count = simple.subn(lambda m: f"{m.group(1)}{fixed_version}{m.group(3)}", text)
    if count:
        return after
    table = re.compile(r'(?m)^(\s*' + re.escape(name) + r'\s*=\s*\{[^}]*version\s*=\s*")([^"]*)(")')
    after, count = table.subn(lambda m: f"{m.group(1)}{fixed_version}{m.group(3)}", text)
    return after if count else None


# ── helpers ───────────────────────────────────────────────────────────────────

def _purl_version(purl: str) -> Optional[str]:
    body = purl.split("?", 1)[0]
    if "@" not in body:
        return None
    from urllib.parse import unquote

    return unquote(body.rsplit("@", 1)[1])


def _purl_name(purl: str) -> str:
    body = purl.split("?", 1)[0].rsplit("@", 1)[0]
    from urllib.parse import unquote

    return unquote(body.rsplit("/", 1)[-1])


def docker_hub_digest(http: Any, image: str, tag: str) -> Optional[str]:
    """The digest Docker Hub serves for a library image's tag, for a ``FROM`` that pins one."""
    repository = image if "/" in image else f"library/{image}"
    if "." in repository.split("/", 1)[0]:
        return None   # another registry: not asked
    data = http.get_json(f"https://hub.docker.com/v2/repositories/{repository}/tags/{tag}", allow_404=True)
    if not data:
        return None
    digest = data.get("digest") or next((i.get("digest") for i in data.get("images") or [] if i.get("digest")), None)
    return str(digest) if digest else None


def group_by_file(edits: List[Edit]) -> Dict[str, Edit]:
    """Edits on the same file folded into one, applied in sequence."""
    out: Dict[str, Edit] = {}
    for edit in edits:
        if edit.path in out:
            prior = out[edit.path]
            out[edit.path] = Edit(path=edit.path, before=prior.before, after=edit.after, description=prior.description + "; " + edit.description,
                                  strategy=prior.strategy if prior.strategy == edit.strategy else "mixed", notes=prior.notes + edit.notes)
        else:
            out[edit.path] = edit
    return out
