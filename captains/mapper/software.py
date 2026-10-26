"""Software on the map: what a workload or a repository is made of, as package URLs
(docs/specs/patching-captain.md §3.1, §3.3).

Every collector that sees a version string hands it here and gets back ``Software``
entries, stored under ``attributes["software"]`` of the node they describe: the images
a task definition runs (``pkg:docker/php@8.3.30-fpm``), a Lambda's runtime and layers
(``pkg:generic/aws-lambda-runtime@python3.10``, ``pkg:generic/bref-layer/php-83-fpm@51``),
an instance's AMI (``pkg:generic/ami@ami-0abc``), what a Dockerfile installs
(``pkg:deb/ubuntu/php8.3-fpm?distro=ubuntu-24.04``), and the packages a manifest or a
lockfile pins (``pkg:composer/laravel/framework@11.2.0``). Each entry says where it was
read (``evidence``) and how it relates to the node (``relation``): the image, its base,
a package the image installs, a runtime, a layer, an AMI, a direct or a transitive
dependency. The patch captain matches these against the advisories; nothing here
decides anything.

``annotate_exposure`` is the other half of "affected": whether a workload faces the
internet, from the map's own edges (an internet-facing load balancer behind an ECS
service, a Lambda function URL, a public IP with a security group open to the world).

Pure functions over text and the dicts the describe calls return; the collectors own
the calls.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import yaml

from infra_map.models import RelationshipEdge, ResourceNode

MAX_ENTRIES = 1500            # per node; a lockfile beyond it is truncated and says so
BREF_ACCOUNT = "534081306603"  # the account Bref publishes its layers from
LAMBDA_RUNTIME_NAME = "aws-lambda-runtime"

# docker-library images whose tag is the upstream version (the fixed tag is the one whose
# upstream version is fixed); anything else keeps its full repository path as the name
LIBRARY_IMAGES = frozenset({
    "php", "node", "python", "ruby", "nginx", "httpd", "golang", "openjdk", "eclipse-temurin", "tomcat", "redis", "postgres",
    "mysql", "mariadb", "memcached", "rabbitmq", "ubuntu", "debian", "alpine", "amazonlinux", "busybox", "caddy", "haproxy", "traefik",
    "wordpress", "drupal", "composer", "rust", "perl", "elixir", "erlang", "dotnet", "mono",
})
UBUNTU_CODENAMES = {"focal": "20.04", "jammy": "22.04", "noble": "24.04", "bionic": "18.04", "oracular": "24.10", "plucky": "25.04"}
DEBIAN_RELEASES = {"10": "buster", "11": "bullseye", "12": "bookworm", "13": "trixie"}
DEBIAN_CODENAMES = frozenset(DEBIAN_RELEASES.values())
DEBIAN_BASED = frozenset({"php", "node", "python", "ruby", "nginx", "httpd", "golang", "openjdk", "tomcat", "redis", "postgres", "mysql", "mariadb",
                          "memcached", "rabbitmq", "composer", "rust", "perl", "elixir", "erlang", "wordpress", "drupal", "caddy", "haproxy"})

MANIFESTS = ("composer.json", "package.json", "pyproject.toml", "go.mod", "Cargo.toml")
LOCKFILES = ("composer.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "go.sum", "Cargo.lock")
DEPLOY_FILES = ("serverless.yml", "serverless.yaml", "template.yml", "template.yaml", "samconfig.toml")
_REQUIREMENTS_RE = re.compile(r"^requirements[\w.-]*\.txt$")


@dataclass
class Software:
    purl: str
    type: str                      # docker | deb | apk | rpm | composer | npm | pypi | golang | cargo | gem | generic
    name: str
    version: Optional[str]
    evidence: str                  # "Dockerfile:3", "composer.lock", "ecs:DescribeTaskDefinition", ...
    relation: str                  # image | base | package | runtime | layer | ami | direct | transitive | dev | tool
    qualifiers: Dict[str, str] = field(default_factory=dict)
    namespace: Optional[str] = None

    def rebuild(self) -> None:
        """Recompute the purl after a qualifier changed."""
        bare = self.name[len(self.namespace) + 1:] if self.namespace and self.name.startswith(self.namespace + "/") else self.name
        self.purl = purl(self.type, bare, self.version, namespace=self.namespace, qualifiers=self.qualifiers)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"purl": self.purl, "type": self.type, "name": self.name, "evidence": self.evidence, "relation": self.relation}
        if self.version:
            out["version"] = self.version
        if self.qualifiers:
            out["qualifiers"] = dict(self.qualifiers)
        return out


# ── purls ─────────────────────────────────────────────────────────────────────

def _segment(value: str) -> str:
    return quote(str(value), safe="")


def purl(type_: str, name: str, version: Optional[str] = None, *, namespace: Optional[str] = None,
         qualifiers: Optional[Mapping[str, Optional[str]]] = None) -> str:
    """A package URL per the spec: ``pkg:<type>/<namespace>/<name>@<version>?<qualifiers>``."""
    path = "/".join(_segment(p) for p in (name or "").split("/") if p)
    if namespace:
        path = "/".join(_segment(p) for p in namespace.split("/") if p) + "/" + path
    out = f"pkg:{type_}/{path}"
    if version:
        out += "@" + quote(str(version), safe="")
    pairs = sorted((k, v) for k, v in (qualifiers or {}).items() if v)
    if pairs:
        out += "?" + "&".join(f"{k}={quote(str(v), safe='/:')}" for k, v in pairs)
    return out


def _entry(type_: str, name: str, version: Optional[str], evidence: str, relation: str, *,
           namespace: Optional[str] = None, qualifiers: Optional[Mapping[str, Optional[str]]] = None) -> Software:
    clean = {k: v for k, v in (qualifiers or {}).items() if v}
    return Software(purl=purl(type_, name, version, namespace=namespace, qualifiers=clean), type=type_,
                    name=f"{namespace}/{name}" if namespace else name, version=version or None, evidence=evidence, relation=relation,
                    qualifiers=clean, namespace=namespace)


def cap(entries: Sequence[Software]) -> Tuple[List[Dict[str, Any]], bool]:
    """The entries as the node stores them, at most ``MAX_ENTRIES``; the flag says whether it was cut."""
    return [e.to_dict() for e in entries[:MAX_ENTRIES]], len(entries) > MAX_ENTRIES


def put_software(attributes: Dict[str, Any], entries: Sequence[Software]) -> None:
    """Write ``software`` (and its count) on a node's attributes; nothing when there is none."""
    if not entries:
        return
    rows, cut = cap(entries)
    attributes["software"] = rows
    attributes["software_count"] = len(entries)
    if cut:
        attributes["software_truncated"] = True


# ── images ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ImageRef:
    registry: Optional[str]
    repository: str          # path without the registry: "library/php", "acme/web"
    tag: Optional[str]
    digest: Optional[str]

    @property
    def short_name(self) -> str:
        return self.repository.rsplit("/", 1)[-1]

    @property
    def is_library(self) -> bool:
        """A docker-library image (``php``, ``docker.io/library/php``, the ECR public mirror)."""
        if self.registry in (None, "docker.io", "index.docker.io", "registry-1.docker.io"):
            return "/" not in self.repository or self.repository.startswith("library/")
        return self.registry == "public.ecr.aws" and self.repository.startswith("docker/library/")

    @property
    def repository_url(self) -> Optional[str]:
        return None if self.is_library else f"{self.registry + '/' if self.registry else ''}{self.repository}"


_IMAGE_RE = re.compile(r"^(?P<ref>[^@\s]+?)(?::(?P<tag>[\w][\w.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")


def parse_image_ref(value: str) -> Optional[ImageRef]:
    """``registry/repo:tag@sha256:…`` in its parts; None for something that is not an image reference."""
    text = (value or "").strip().strip("\"'")
    if not text or "$" in text or " " in text:
        return None
    match = _IMAGE_RE.match(text)
    if not match:
        return None
    ref, tag, digest = match.group("ref"), match.group("tag"), match.group("digest")
    # a port in the registry looks like a tag on a single-segment name: "localhost:5000/x" has no tag part here
    registry: Optional[str] = None
    repository = ref
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        registry, repository = ref.split("/", 1)
    if not repository:
        return None
    return ImageRef(registry=registry, repository=repository, tag=tag, digest=digest)


def image_software(value: str, evidence: str, relation: str = "image", *, digest: Optional[str] = None,
                   extra: Optional[Mapping[str, Optional[str]]] = None) -> Optional[Software]:
    ref = parse_image_ref(value)
    if ref is None:
        return None
    name = ref.short_name if ref.is_library else ref.repository
    qualifiers: Dict[str, Optional[str]] = {"repository_url": ref.repository_url, "digest": digest or ref.digest, **dict(extra or {})}
    return _entry("docker", name, ref.tag, evidence, relation, qualifiers=qualifiers)


def ecr_parts(value: str) -> Optional[Tuple[str, str, str, Optional[str]]]:
    """``(account, region, repository, tag)`` of an ECR image reference, else None."""
    ref = parse_image_ref(value)
    if ref is None or not ref.registry:
        return None
    match = re.match(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$", ref.registry)
    if not match:
        return None
    return match.group(1), match.group(2), ref.repository, ref.tag


def base_distro(ref: ImageRef) -> Optional[Tuple[str, str, Optional[str]]]:
    """``(package type, distro, release)`` of the OS inside a base image, from its name and tag:
    ``ubuntu:24.04`` → deb/ubuntu/24.04; ``php:8.3-fpm`` → deb/debian (bookworm by default);
    ``php:8.3-fpm-alpine3.20`` → apk/alpine/3.20; ``amazonlinux:2023`` → rpm/amazonlinux/2023."""
    name = ref.short_name if ref.is_library else ref.repository.rsplit("/", 1)[-1]
    tag = (ref.tag or "").lower()
    if name == "ubuntu":
        release = UBUNTU_CODENAMES.get(tag, tag if re.match(r"^\d\d\.\d\d$", tag) else None)
        return ("deb", "ubuntu", release)
    if name == "debian":
        code = tag.split("-", 1)[0]
        return ("deb", "debian", DEBIAN_RELEASES.get(code, code if code in DEBIAN_CODENAMES else None))
    if name == "alpine":
        return ("apk", "alpine", tag if re.match(r"^\d+\.\d+", tag) else None)
    if name == "amazonlinux":
        return ("rpm", "amazonlinux", tag or None)
    alpine = re.search(r"alpine(\d+\.\d+)?", tag)
    if alpine:
        return ("apk", "alpine", alpine.group(1))
    for code in DEBIAN_CODENAMES:
        if code in tag.split("-"):
            return ("deb", "debian", code)
    if name in DEBIAN_BASED:
        return ("deb", "debian", None)
    return None


# ── Dockerfiles ───────────────────────────────────────────────────────────────

@dataclass
class DockerStage:
    index: int
    image: Optional[str]           # None when it builds on an earlier stage
    name: Optional[str]
    line: int
    distro: Optional[Tuple[str, str, Optional[str]]] = None
    parent: Optional[int] = None   # the earlier stage it builds on, when image is None


@dataclass
class DockerfileSoftware:
    stages: List[DockerStage] = field(default_factory=list)
    software: List[Software] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def base_images(self) -> List[str]:
        return [s.image for s in self.stages if s.image]


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-+][^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _substitute(text: str, variables: Mapping[str, str]) -> str:
    def repl(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        value = variables.get(name)
        if value is None:
            default = re.match(r"\$\{[^:}]+:?-([^}]*)\}", match.group(0))
            return default.group(1) if default else match.group(0)
        return value
    return _VAR_RE.sub(repl, text)


def _instructions(text: str) -> List[Tuple[int, str, str]]:
    """``(line, INSTRUCTION, arguments)`` with continuations joined and comments dropped."""
    out: List[Tuple[int, str, str]] = []
    buffer: List[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and stripped.startswith("#"):
            continue
        if not buffer:
            start = number
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1])
            continue
        buffer.append(stripped)
        whole = " ".join(buffer).strip()
        buffer = []
        parts = whole.split(None, 1)
        if parts:
            out.append((start, parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    if buffer:
        whole = " ".join(buffer).strip()
        parts = whole.split(None, 1)
        if parts:
            out.append((start, parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return out


def _shell_commands(script: str) -> List[List[str]]:
    """The commands of a RUN line, split on the shell's joiners, each as its words."""
    if script.lstrip().startswith("["):
        try:
            words = json.loads(script)
            return [[str(w) for w in words]] if isinstance(words, list) else []
        except ValueError:
            return []
    commands: List[List[str]] = []
    for chunk in re.split(r"&&|\|\||;|\|", script):
        try:
            words = shlex.split(chunk, comments=False, posix=True)
        except ValueError:
            words = chunk.split()
        if words:
            commands.append(words)
    return commands


def _strip_wrappers(words: List[str]) -> List[str]:
    """``sudo``, ``set -eux``, ``DEBIAN_FRONTEND=... apt-get`` → the command itself."""
    out = list(words)
    while out and (out[0] in ("sudo", "exec", "env") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0])):
        out.pop(0)
    return out


def _packages_from(words: Sequence[str], *, separators: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
    found: List[Tuple[str, Optional[str]]] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        if word.startswith("-"):
            if word in ("-o", "-t", "--target-release", "-c", "--index-url", "-i", "--extra-index-url", "--prefix", "--cache-dir", "--repository", "-X", "--virtual"):
                skip_next = True
            continue
        if word in ("install", "add", "require", "i", "global", "update", "upgrade") or word.startswith(("http://", "https://", "./", "/", "git+")) or word.endswith((".txt", ".whl", ".tar.gz", ".deb", ".apk", ".zip")):
            continue
        name, version = word, None
        for sep in separators:
            if sep in word:
                name, version = word.split(sep, 1)
                break
        name = name.strip("\"'")
        if not name or name.startswith("$"):
            continue
        found.append((name, (version or "").strip("\"'") or None))
    return found


def _run_software(words: List[str], *, stage: DockerStage, evidence: str) -> List[Software]:
    words = _strip_wrappers(words)
    if not words:
        return []
    head = words[0]
    distro = stage.distro or ("deb", "debian", None)
    qualifiers: Dict[str, Optional[str]] = {"stage": str(stage.index)}
    out: List[Software] = []

    def os_packages(type_: str, namespace: str, release: Optional[str], names: List[Tuple[str, Optional[str]]], *, sep_release: str) -> None:
        for name, version in names:
            q = {**qualifiers, "distro": f"{namespace}-{release}" if release else namespace}
            out.append(_entry(type_, name, version, evidence, "package", namespace=namespace, qualifiers=q))

    if head in ("apt-get", "apt") and "install" in words:
        pkgs = _packages_from(words[words.index("install") + 1:], separators=("=",))
        if distro[0] == "deb":
            os_packages("deb", distro[1], distro[2], pkgs, sep_release="-")
        else:
            os_packages("deb", "debian", None, pkgs, sep_release="-")
    elif head == "apk" and "add" in words:
        pkgs = _packages_from(words[words.index("add") + 1:], separators=("=", "~=", ">"))
        os_packages("apk", "alpine", distro[2] if distro[0] == "apk" else None, pkgs, sep_release="-")
    elif head in ("yum", "dnf", "microdnf") and "install" in words:
        # an rpm name may carry its version after a dash, which a name may too: keep the word whole
        pkgs = [(n, None) for n, _ in _packages_from(words[words.index("install") + 1:], separators=())]
        os_packages("rpm", distro[1] if distro[0] == "rpm" else "amazonlinux", distro[2] if distro[0] == "rpm" else None, pkgs, sep_release="-")
    elif (head in ("pip", "pip3") and "install" in words) or (head in ("python", "python3") and words[1:3] == ["-m", "pip"] and "install" in words):
        after = words[words.index("install") + 1:]
        for name, version in _packages_from(after, separators=("==", "===")):
            if name.startswith("-"):
                continue
            base = re.split(r"[<>~!\[;]", name, maxsplit=1)[0]
            out.append(_entry("pypi", base.lower().replace("_", "-"), version, evidence, "package", qualifiers=qualifiers))
    elif head == "composer" and "require" in words:
        after = words[words.index("require") + 1:]
        for name, version in _packages_from(after, separators=(":",)):
            if "/" not in name:
                continue
            vendor, pkg = name.split("/", 1)
            relation = "tool" if "global" in words else "package"
            out.append(_entry("composer", pkg.lower(), version if version and re.match(r"^\d", version) else None, evidence, relation, namespace=vendor.lower(),
                              qualifiers={**qualifiers, "constraint": version if version and not re.match(r"^\d", version) else None}))
    elif head in ("npm", "pnpm", "yarn") and any(w in words for w in ("install", "i", "add", "global")) and any(w in words for w in ("-g", "--global", "global")):
        after = words[1:]
        for name, version in _packages_from(after, separators=("@",)):
            if name.startswith("@") or not name:
                continue
            out.append(_entry("npm", name, version, evidence, "tool", qualifiers=qualifiers))
    elif head == "gem" and "install" in words:
        for name, version in _packages_from(words[words.index("install") + 1:], separators=(":",)):
            out.append(_entry("gem", name, version, evidence, "package", qualifiers=qualifiers))
    return out


def parse_dockerfile(text: str, *, path: str = "Dockerfile") -> DockerfileSoftware:
    """Every stage's base image and what its RUN lines install, with the line it came from.
    The last stage is the image that runs; its entries carry ``final=true``."""
    result = DockerfileSoftware()
    variables: Dict[str, str] = {}
    stage: Optional[DockerStage] = None
    pending: List[Tuple[DockerStage, Software]] = []
    stage_names: Set[str] = set()
    for line, instruction, args in _instructions(text):
        if instruction in ("ARG", "ENV"):
            for token in shlex.split(args, posix=True) if "=" in args else [args]:
                if "=" in token:
                    key, value = token.split("=", 1)
                    variables.setdefault(key.strip(), value.strip().strip("\"'"))
                elif instruction == "ENV" and " " in args:
                    key, value = args.split(None, 1)
                    variables.setdefault(key.strip(), value.strip().strip("\"'"))
            continue
        if instruction == "FROM":
            words = [w for w in args.split() if not w.startswith("--")]
            if not words:
                continue
            image = _substitute(words[0], variables)
            name = words[2] if len(words) >= 3 and words[1].lower() == "as" else None
            index = len(result.stages)
            if image in stage_names or image.isdigit():
                parent = next((s.index for s in result.stages if s.name == image), int(image) if image.isdigit() and int(image) < index else None)
                parent_stage = result.stages[parent] if parent is not None else None
                stage = DockerStage(index=index, image=None, name=name, line=line, distro=parent_stage.distro if parent_stage else None, parent=parent)
            elif "$" in image:
                stage = DockerStage(index=index, image=None, name=name, line=line)
                result.notes.append(f"{path}:{line}: FROM {words[0]} depends on a build argument without a default")
            else:
                ref = parse_image_ref(image)
                stage = DockerStage(index=index, image=image, name=name, line=line, distro=base_distro(ref) if ref else None)
                entry = image_software(image, f"{path}:{line}", "base", extra={"stage": str(index)})
                if entry is not None:
                    pending.append((stage, entry))
            if name:
                stage_names.add(name)
            result.stages.append(stage)
            continue
        if instruction == "RUN" and stage is not None:
            for command in _shell_commands(_substitute(args, variables)):
                for entry in _run_software(command, stage=stage, evidence=f"{path}:{line}"):
                    pending.append((stage, entry))
    # a stage built FROM an earlier stage inherits that stage's OS, so its packages count as the final image's
    final_stages = _final_stages(result.stages)
    for st, entry in pending:
        if st.index in final_stages:
            entry.qualifiers["final"] = "true"
            entry.rebuild()
        result.software.append(entry)
    return result


def _final_stages(stages: Sequence[DockerStage]) -> Set[int]:
    """The last stage and, when it builds FROM an earlier stage, that chain: their software is
    what the image runs. ``COPY --from`` copies files, not packages, so it is not followed."""
    if not stages:
        return set()
    chain: Set[int] = set()
    index = stages[-1].index
    while True:
        chain.add(index)
        stage = stages[index]
        if stage.image is not None:
            break
        previous = _parent_stage(stages, stage)
        if previous is None or previous in chain:
            break
        index = previous
    return chain


def _parent_stage(stages: Sequence[DockerStage], stage: DockerStage) -> Optional[int]:
    return stage.parent


# ── manifests and lockfiles ───────────────────────────────────────────────────

def classify_software_file(path: str) -> Optional[str]:
    """``manifest``, ``lockfile`` or ``deploy`` for a path the mapper should read for software; else None."""
    filename = path.rsplit("/", 1)[-1]
    lowered = filename.lower()
    if filename in MANIFESTS or _REQUIREMENTS_RE.match(lowered):
        return "manifest"
    if filename in LOCKFILES:
        return "lockfile"
    if lowered in DEPLOY_FILES:
        return "deploy"
    return None


def _pep508(spec: str) -> Tuple[str, Optional[str]]:
    """``requests[security]>=2.31,<3 ; python_version>'3'`` → (``requests``, pinned version or None)."""
    body = spec.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", body)
    if not match:
        return "", None
    name = match.group(1).lower().replace("_", "-")
    pin = re.search(r"===?\s*([A-Za-z0-9][A-Za-z0-9.+!*-]*)", body)
    return name, (pin.group(1) if pin and not pin.group(1).endswith("*") else None)


def _npm_parts(name: str) -> Tuple[Optional[str], str]:
    if name.startswith("@") and "/" in name:
        scope, rest = name[1:].split("/", 1)
        return "%40" + scope, rest
    return None, name


def _npm_entry(name: str, version: Optional[str], evidence: str, relation: str) -> Software:
    scope, bare = _npm_parts(name)
    entry = _entry("npm", bare, version, evidence, relation)
    if scope:
        entry.purl = f"pkg:npm/{scope}/{_segment(bare)}" + (f"@{quote(version, safe='')}" if version else "")
        entry.name = name
    return entry


def _exact(version: Any) -> Optional[str]:
    """A version that is a pin, not a range: ``11.2.0``, ``=1.0.203``, ``v7.1.3``; a bare ``1`` is a
    caret range to npm, composer and cargo alike."""
    text = str(version or "").strip().lstrip("=")
    return text if re.match(r"^v?\d+\.\d+(\.\d+)*([.+-][0-9A-Za-z.+-]+)?$", text) else None


def parse_manifest(path: str, text: str) -> List[Software]:
    """The direct dependencies a manifest declares; a version is kept only when it is an exact pin."""
    filename = path.rsplit("/", 1)[-1]
    evidence = path
    out: List[Software] = []
    try:
        if filename == "composer.json":
            data = json.loads(text)
            for section, relation in (("require", "direct"), ("require-dev", "dev")):
                for name, constraint in (data.get(section) or {}).items():
                    if "/" not in name:
                        continue  # php, ext-*: the platform, not a package
                    vendor, pkg = name.lower().split("/", 1)
                    out.append(_entry("composer", pkg, _exact(constraint), evidence, relation, namespace=vendor, qualifiers={"constraint": None if _exact(constraint) else str(constraint)}))
        elif filename == "package.json":
            data = json.loads(text)
            for section, relation in (("dependencies", "direct"), ("devDependencies", "dev"), ("optionalDependencies", "direct")):
                for name, constraint in (data.get(section) or {}).items():
                    out.append(_npm_entry(name, _exact(constraint), evidence, relation))
        elif _REQUIREMENTS_RE.match(filename.lower()):
            for raw in text.splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith(("-", "git+", "http")) or "://" in line:
                    continue
                name, version = _pep508(line.rstrip("\\").strip())
                if name:
                    out.append(_entry("pypi", name, version, evidence, "direct"))
        elif filename == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            for spec in project.get("dependencies") or []:
                name, version = _pep508(str(spec))
                if name:
                    out.append(_entry("pypi", name, version, evidence, "direct"))
            for group in (project.get("optional-dependencies") or {}).values():
                for spec in group or []:
                    name, version = _pep508(str(spec))
                    if name:
                        out.append(_entry("pypi", name, version, evidence, "dev"))
            poetry = ((data.get("tool") or {}).get("poetry") or {})
            for section, relation in (("dependencies", "direct"), ("dev-dependencies", "dev")):
                for name, constraint in (poetry.get(section) or {}).items():
                    if name.lower() == "python":
                        continue
                    version = constraint.get("version") if isinstance(constraint, dict) else constraint
                    out.append(_entry("pypi", name.lower().replace("_", "-"), _exact(version), evidence, relation))
            for group in ((poetry.get("group") or {}).values()):
                for name, constraint in ((group or {}).get("dependencies") or {}).items():
                    version = constraint.get("version") if isinstance(constraint, dict) else constraint
                    out.append(_entry("pypi", name.lower().replace("_", "-"), _exact(version), evidence, "dev"))
        elif filename == "go.mod":
            block = False
            for raw in text.splitlines():
                line = raw.split("//", 1)[0].strip()
                if line.startswith("require ("):
                    block = True
                    continue
                if block and line == ")":
                    block = False
                    continue
                match = re.match(r"^(?:require\s+)?(\S+)\s+(v\S+)", line) if (block or line.startswith("require ")) else None
                if match:
                    out.append(_entry("golang", match.group(1), match.group(2), evidence, "dev" if "// indirect" in raw else "direct"))
        elif filename == "Cargo.toml":
            data = tomllib.loads(text)
            for section, relation in (("dependencies", "direct"), ("dev-dependencies", "dev"), ("build-dependencies", "dev")):
                for name, constraint in (data.get(section) or {}).items():
                    version = constraint.get("version") if isinstance(constraint, dict) else constraint
                    out.append(_entry("cargo", name, _exact(version), evidence, relation))
    except (ValueError, TypeError, AttributeError):
        return []
    return out


def parse_lockfile(path: str, text: str, direct: Optional[Set[str]] = None) -> List[Software]:
    """Every package a lockfile resolves, with its exact version; ``direct`` (names from the sibling
    manifest) decides direct against transitive. Dev-only packages are ``dev``."""
    filename = path.rsplit("/", 1)[-1]
    evidence = path
    direct = {d.lower() for d in (direct or set())}
    out: List[Software] = []

    def relation(name: str, dev: bool = False) -> str:
        if dev:
            return "dev"
        return "direct" if name.lower() in direct else "transitive"

    try:
        if filename == "composer.lock":
            data = json.loads(text)
            for section, dev in (("packages", False), ("packages-dev", True)):
                for pkg in data.get(section) or []:
                    name = str(pkg.get("name") or "").lower()
                    if "/" not in name:
                        continue
                    vendor, bare = name.split("/", 1)
                    out.append(_entry("composer", bare, str(pkg.get("version") or "").lstrip("v") or None, evidence, relation(name, dev), namespace=vendor))
        elif filename == "package-lock.json":
            data = json.loads(text)
            packages = data.get("packages")
            if isinstance(packages, dict):
                root_direct = set((packages.get("") or {}).get("dependencies") or {}) | set((packages.get("") or {}).get("devDependencies") or {})
                for key, pkg in packages.items():
                    if not key or not isinstance(pkg, dict) or not pkg.get("version"):
                        continue
                    name = key.rsplit("node_modules/", 1)[-1]
                    out.append(_npm_entry(name, str(pkg["version"]), evidence, "dev" if pkg.get("dev") else ("direct" if name in root_direct or name.lower() in direct else "transitive")))
            else:
                def walk(deps: Mapping[str, Any], top: bool) -> None:
                    for name, pkg in (deps or {}).items():
                        if not isinstance(pkg, dict) or not pkg.get("version"):
                            continue
                        out.append(_npm_entry(name, str(pkg["version"]), evidence, "dev" if pkg.get("dev") else ("direct" if top or name.lower() in direct else "transitive")))
                        walk(pkg.get("dependencies") or {}, False)
                walk(data.get("dependencies") or {}, True)
        elif filename == "yarn.lock":
            current: List[str] = []
            for raw in text.splitlines():
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                if not raw.startswith((" ", "\t")):
                    current = [re.sub(r"@(npm:)?[^@]*$", "", k.strip().strip('"')) for k in raw.rstrip(":").split(",")]
                    continue
                match = re.match(r'^\s+version:?\s+"?([^"\s]+)"?', raw)
                if match and current:
                    seen: Set[str] = set()
                    for name in current:
                        if name and name not in seen:
                            seen.add(name)
                            out.append(_npm_entry(name, match.group(1), evidence, relation(name)))
        elif filename == "pnpm-lock.yaml":
            data = yaml.safe_load(text) or {}
            importers = data.get("importers") or {}
            root = importers.get(".") if isinstance(importers, dict) else None
            root_direct = set((root or {}).get("dependencies") or {}) | set((data.get("dependencies") or {}))
            dev_names = set((root or {}).get("devDependencies") or {}) | set((data.get("devDependencies") or {}))
            for key in (data.get("packages") or {}):
                text_key = str(key).lstrip("/")
                match = re.match(r"^(@?[^@]+)@([^(]+)", text_key)
                if not match:
                    continue
                name, version = match.group(1), match.group(2)
                out.append(_npm_entry(name, version, evidence, "dev" if name in dev_names else ("direct" if name in root_direct or name.lower() in direct else "transitive")))
        elif filename in ("uv.lock", "poetry.lock", "Cargo.lock"):
            data = tomllib.loads(text)
            type_ = "cargo" if filename == "Cargo.lock" else "pypi"
            for pkg in data.get("package") or []:
                name = str(pkg.get("name") or "")
                version = str(pkg.get("version") or "") or None
                source = pkg.get("source") if isinstance(pkg.get("source"), dict) else {}
                if not name or source.get("editable") or source.get("virtual"):
                    continue
                dev = str(pkg.get("category") or "") == "dev"
                out.append(_entry(type_, name.lower().replace("_", "-") if type_ == "pypi" else name, version, evidence, relation(name, dev)))
        elif filename == "go.sum":
            seen: Set[Tuple[str, str]] = set()
            for raw in text.splitlines():
                parts = raw.split()
                if len(parts) < 2:
                    continue
                module, version = parts[0], parts[1].removesuffix("/go.mod")
                if (module, version) in seen:
                    continue
                seen.add((module, version))
                out.append(_entry("golang", module, version, evidence, relation(module)))
    except (ValueError, TypeError, AttributeError, yaml.YAMLError):
        return []
    return out


def direct_names(manifest_entries: Iterable[Software]) -> Set[str]:
    return {e.name.lower() for e in manifest_entries}


# ── deploy descriptors ────────────────────────────────────────────────────────

def runtime_software(runtime: Any, evidence: str) -> Optional[Software]:
    text = str(runtime or "").strip()
    return _entry("generic", LAMBDA_RUNTIME_NAME, text, evidence, "runtime") if text else None


_LAYER_ARN_RE = re.compile(r"^arn:aws:lambda:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):layer:(?P<name>[A-Za-z0-9_-]+):(?P<version>\d+)$")
_BREF_SHORT_RE = re.compile(r"\$\{bref(?:-extra)?:layer\.([A-Za-z0-9_-]+)\}")


def layer_software(layer: Any, evidence: str) -> Optional[Software]:
    """A layer ARN (``…:layer:php-83-fpm:51``) or Bref's serverless shorthand (``${bref:layer.php-83-fpm}``)."""
    text = str(layer or "").strip()
    match = _LAYER_ARN_RE.match(text)
    if match:
        bref = match.group("account") == BREF_ACCOUNT
        return _entry("generic", match.group("name"), match.group("version"), evidence, "layer", namespace="bref-layer" if bref else "lambda-layer",
                      qualifiers={"arn": text})
    short = _BREF_SHORT_RE.match(text)
    if short:
        return _entry("generic", short.group(1), None, evidence, "layer", namespace="bref-layer", qualifiers={"resolved_by": "serverless-bref-plugin"})
    return None


def parse_deploy(path: str, text: str) -> List[Software]:
    """Runtimes and layers in a ``serverless.yml`` or a SAM template."""
    filename = path.rsplit("/", 1)[-1].lower()
    out: List[Software] = []
    if filename == "samconfig.toml":
        return out
    try:
        data = yaml.safe_load(_cfn_safe(text)) or {}
    except yaml.YAMLError:
        return out
    if not isinstance(data, dict):
        return out
    if filename.startswith("serverless"):
        provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
        default_runtime = provider.get("runtime")
        default_layers = provider.get("layers") or []
        functions = data.get("functions") if isinstance(data.get("functions"), dict) else {}
        seen: Set[str] = set()
        for fn_name, fn in functions.items():
            fn = fn if isinstance(fn, dict) else {}
            evidence = f"{path}:functions.{fn_name}"
            runtime = fn.get("runtime", default_runtime)
            if runtime and str(runtime) not in seen:
                seen.add(str(runtime))
                entry = runtime_software(runtime, evidence)
                if entry:
                    out.append(entry)
            image = fn.get("image")
            # layers do not apply to a container-image function, so the provider's defaults are not its
            for layer in ([] if image else (fn.get("layers") or default_layers)):
                entry = layer_software(layer, evidence)
                if entry and entry.purl not in seen:
                    seen.add(entry.purl)
                    out.append(entry)
            image_ref = image.get("uri") if isinstance(image, dict) else image
            if isinstance(image_ref, str):
                entry = image_software(image_ref, evidence, "image")
                if entry:
                    out.append(entry)
        if not functions and default_runtime:
            entry = runtime_software(default_runtime, f"{path}:provider")
            if entry:
                out.append(entry)
        return out
    # a SAM / CloudFormation template
    globals_fn = ((data.get("Globals") or {}).get("Function") or {}) if isinstance(data.get("Globals"), dict) else {}
    for logical, resource in (data.get("Resources") or {}).items():
        if not isinstance(resource, dict) or resource.get("Type") not in ("AWS::Serverless::Function", "AWS::Lambda::Function"):
            continue
        props = resource.get("Properties") if isinstance(resource.get("Properties"), dict) else {}
        evidence = f"{path}:Resources.{logical}"
        runtime = props.get("Runtime", globals_fn.get("Runtime"))
        entry = runtime_software(runtime, evidence) if isinstance(runtime, str) else None
        if entry:
            out.append(entry)
        for layer in (props.get("Layers") or globals_fn.get("Layers") or []):
            entry = layer_software(layer, evidence)
            if entry:
                out.append(entry)
        image = props.get("ImageUri")
        if isinstance(image, str):
            entry = image_software(image, evidence, "image")
            if entry:
                out.append(entry)
    return out


def _cfn_safe(text: str) -> str:
    """CloudFormation's ``!Ref``/``!Sub`` tags as plain scalars, so PyYAML's safe loader reads the file."""
    return re.sub(r"!(Ref|Sub|GetAtt|Join|Select|FindInMap|If|ImportValue|Split|Base64)\b", "", text)


def terraform_software(parsed: Mapping[str, Any], path: str) -> List[Software]:
    """Runtimes, layers, images and AMIs in Terraform: ``aws_lambda_function``, ``aws_instance``,
    ``aws_launch_template``. Values that are expressions (``var.ami``) are skipped; a filter on
    an ``aws_ami`` data source is a choice kept in code, recorded as its name pattern."""
    out: List[Software] = []
    for entry in parsed.get("resource", []) or []:
        if not isinstance(entry, dict):
            continue
        for resource_type, resources in entry.items():
            if not isinstance(resources, dict):
                continue
            for name, attrs in resources.items():
                if not isinstance(attrs, dict):
                    continue
                evidence = f"{path}:{resource_type}.{name}"
                if resource_type == "aws_lambda_function":
                    runtime = _literal(attrs.get("runtime"))
                    if runtime:
                        out.append(runtime_software(runtime, evidence))  # type: ignore[arg-type]
                    for layer in _literal_list(attrs.get("layers")):
                        layer_entry = layer_software(layer, evidence)
                        if layer_entry:
                            out.append(layer_entry)
                    image = _literal(attrs.get("image_uri"))
                    if image:
                        image_entry = image_software(image, evidence, "image")
                        if image_entry:
                            out.append(image_entry)
                elif resource_type == "aws_instance":
                    ami = _literal(attrs.get("ami"))
                    if ami and ami.startswith("ami-"):
                        out.append(_entry("generic", "ami", ami, evidence, "ami"))
                elif resource_type == "aws_launch_template":
                    ami = _literal(attrs.get("image_id"))
                    if ami and ami.startswith("ami-"):
                        out.append(_entry("generic", "ami", ami, evidence, "ami"))
    for entry in parsed.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        for data_type, sources in entry.items():
            if data_type != "aws_ami" or not isinstance(sources, dict):
                continue
            for name, attrs in sources.items():
                if not isinstance(attrs, dict):
                    continue
                pattern = None
                for flt in attrs.get("filter") or []:
                    if isinstance(flt, dict) and _literal(flt.get("name")) == "name":
                        values = _literal_list(flt.get("values"))
                        pattern = values[0] if values else None
                if pattern:
                    out.append(_entry("generic", "ami", None, f"{path}:data.aws_ami.{name}", "ami", qualifiers={"filter": pattern, "most_recent": str(_literal(attrs.get("most_recent")) or "").lower() or None}))
    return out


def _literal(value: Any) -> Optional[str]:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if not isinstance(value, str):
        return None
    text = value.strip().strip('"')
    return None if "${" in text or not text else text


def _literal_list(value: Any) -> List[str]:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if isinstance(value, str):
        return [v for v in [_literal(value)] if v]
    return [v for v in (_literal(item) for item in (value or [])) if v]


# ── the AWS side ──────────────────────────────────────────────────────────────

DigestResolver = Callable[[str, str, str, str], Optional[str]]   # (account, region, repository, tag) -> sha256 digest


def task_definition_software(task_def: Mapping[str, Any], resolve_digest: Optional[DigestResolver] = None) -> Tuple[List[Dict[str, Any]], List[Software]]:
    """``(images, software)`` of a task definition: one image per container, resolved to its
    digest through ECR when the resolver knows it."""
    images: List[Dict[str, Any]] = []
    out: List[Software] = []
    for container in task_def.get("containerDefinitions") or []:
        image = str(container.get("image") or "").strip()
        if not image:
            continue
        digest = None
        parts = ecr_parts(image)
        if parts and resolve_digest is not None and parts[3]:
            try:
                digest = resolve_digest(*parts)  # type: ignore[misc]
            except Exception:  # noqa: BLE001 - a registry that will not answer leaves the tag alone
                digest = None
        images.append({"container": container.get("name"), "image": image, **({"digest": digest} if digest else {})})
        entry = image_software(image, "ecs:DescribeTaskDefinition", "image", digest=digest, extra={"container": container.get("name")})
        if entry:
            out.append(entry)
    return images, out


def lambda_software(function: Mapping[str, Any]) -> List[Software]:
    """A function's runtime and layers as ``ListFunctions`` reports them. A container-image
    function says so; its image needs ``lambda:GetFunction``, which the access model forbids."""
    out: List[Software] = []
    entry = runtime_software(function.get("Runtime"), "lambda:ListFunctions")
    if entry:
        out.append(entry)
    for layer in function.get("Layers") or []:
        arn = layer.get("Arn") if isinstance(layer, dict) else layer
        layer_entry = layer_software(arn, "lambda:ListFunctions")
        if layer_entry:
            out.append(layer_entry)
    return out


def instance_software(instance: Mapping[str, Any]) -> List[Software]:
    ami = str(instance.get("ImageId") or "").strip()
    return [_entry("generic", "ami", ami, "ec2:DescribeInstances", "ami")] if ami else []


def ssm_inventory_software(entries: Iterable[Mapping[str, Any]], *, platform: Optional[str] = None) -> List[Software]:
    """``AWS:Application`` inventory rows as packages: rpm on Amazon Linux, RHEL and friends, deb on
    Ubuntu and Debian; the platform comes from ``AWS:InstanceInformation``."""
    text = (platform or "").lower()
    type_, namespace = ("deb", "ubuntu") if "ubuntu" in text else ("deb", "debian") if "debian" in text else ("rpm", "amazonlinux" if "amazon" in text else "rhel" if "red hat" in text or "rhel" in text else "linux")
    out: List[Software] = []
    for row in entries:
        name = str(row.get("Name") or "").strip()
        version = str(row.get("Version") or "").strip() or None
        if not name:
            continue
        arch = str(row.get("Architecture") or "").strip() or None
        out.append(_entry(type_, name, version, "ssm:ListInventoryEntries", "package", namespace=namespace, qualifiers={"arch": arch}))
    return out


# ── exposure ──────────────────────────────────────────────────────────────────

PUBLIC_CIDRS = frozenset({"0.0.0.0/0", "::/0"})


def public_ingress(security_group: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The ingress rules of a ``DescribeSecurityGroups`` entry open to the whole internet."""
    rules: List[Dict[str, Any]] = []
    for permission in security_group.get("IpPermissions") or []:
        ranges = [r.get("CidrIp") for r in permission.get("IpRanges") or []] + [r.get("CidrIpv6") for r in permission.get("Ipv6Ranges") or []]
        open_ranges = [r for r in ranges if r in PUBLIC_CIDRS]
        if not open_ranges:
            continue
        rules.append({"protocol": permission.get("IpProtocol"), "from_port": permission.get("FromPort"), "to_port": permission.get("ToPort"), "cidrs": open_ranges})
    return rules


def annotate_exposure(nodes: Sequence[ResourceNode], edges: Sequence[RelationshipEdge]) -> None:
    """Set ``exposed`` and ``exposure`` (how, with the evidence) on every workload: an ECS service
    behind an internet-facing load balancer and the task definition it runs; a Lambda with a
    function URL; an instance with a public IP behind a security group open to the world."""
    by_id = {n.resource_id: n for n in nodes}
    outgoing: Dict[str, List[RelationshipEdge]] = {}
    incoming: Dict[str, List[RelationshipEdge]] = {}
    for edge in edges:
        outgoing.setdefault(edge.from_resource_id, []).append(edge)
        incoming.setdefault(edge.to_resource_id, []).append(edge)

    def internet_facing_lbs(service_id: str) -> List[str]:
        found: List[str] = []
        for exposes in outgoing.get(service_id, []):
            if exposes.relationship_type != "exposes":
                continue
            target_group = exposes.to_resource_id
            lb_ids = [e.to_resource_id for e in outgoing.get(target_group, []) if e.relationship_type == "attached_to"]
            lb_ids += [e.from_resource_id for e in incoming.get(target_group, []) if e.relationship_type == "routes_to"]
            for lb_id in lb_ids:
                lb = by_id.get(lb_id)
                if lb is not None and lb.type == "elbv2_load_balancer" and str((lb.attributes or {}).get("scheme") or "").lower() == "internet-facing":
                    found.append(lb.name or lb_id)
        return found

    def open_groups(node_id: str) -> List[str]:
        found: List[str] = []
        for edge in outgoing.get(node_id, []):
            if edge.relationship_type != "secured_by":
                continue
            group = by_id.get(edge.to_resource_id)
            if group is not None and group.type == "ec2_security_group" and (group.attributes or {}).get("publicIngress"):
                found.append(group.name or group.resource_id)
        return found

    def mark(node: ResourceNode, via: List[Dict[str, Any]]) -> None:
        node.attributes["exposed"] = bool(via)
        node.attributes["exposure"] = {"exposed": bool(via), "via": via}

    service_exposure: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        if node.type == "ecs_service":
            via = [{"kind": "load_balancer", "name": name, "evidence": "elbv2:DescribeLoadBalancers scheme=internet-facing"} for name in internet_facing_lbs(node.resource_id)]
            service_exposure[node.resource_id] = via
            mark(node, via)
        elif node.type == "lambda_function":
            url = (node.attributes or {}).get("functionUrl")
            via = [{"kind": "function_url", "name": url, "evidence": "lambda:ListFunctionUrlConfigs"}] if url else []
            mark(node, via)
        elif node.type == "ec2_instance":
            attrs = node.attributes or {}
            via = []
            if attrs.get("publicIp"):
                groups = open_groups(node.resource_id)
                if groups:
                    via = [{"kind": "public_ip", "name": attrs.get("publicIp"), "evidence": f"ec2:DescribeInstances; security group {', '.join(groups)} open to 0.0.0.0/0"}]
            mark(node, via)
    for node in nodes:
        if node.type != "ecs_task_definition":
            continue
        # the collector links task definition → service (uses_in); older maps have the edge the other way
        services = [e.to_resource_id for e in outgoing.get(node.resource_id, []) if e.relationship_type == "uses_in"]
        services += [e.from_resource_id for e in incoming.get(node.resource_id, []) if e.relationship_type == "uses_in"]
        via: List[Dict[str, Any]] = []
        for service_id in services:
            for item in service_exposure.get(service_id, []):
                via.append({**item, "service": by_id[service_id].name})
        mark(node, via)


# ── a repository's files, together ────────────────────────────────────────────

def repository_software(files: Mapping[str, Tuple[str, str]]) -> Dict[str, List[Software]]:
    """``{path: entries}`` for a repository's software files (``{path: (classification, text)}``):
    Dockerfiles, manifests, lockfiles and deploy descriptors, with a lockfile's direct set taken
    from the manifest beside it."""
    manifests: Dict[str, Set[str]] = {}
    out: Dict[str, List[Software]] = {}
    for path, (classification, text) in files.items():
        if classification == "manifest":
            entries = parse_manifest(path, text)
            out[path] = entries
            directory = posixpath.dirname(path)
            manifests.setdefault(directory, set()).update(direct_names(entries))
    for path, (classification, text) in files.items():
        if classification == "dockerfile":
            out[path] = parse_dockerfile(text, path=path).software
        elif classification == "lockfile":
            out[path] = parse_lockfile(path, text, manifests.get(posixpath.dirname(path)))
        elif classification == "deploy":
            out[path] = parse_deploy(path, text)
    return out
