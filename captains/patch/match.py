"""The match: advisory × software × node → a finding (docs/specs/patching-captain.md §3.3).

Every software entry on the map is held against every advisory that names its package,
its distro package or its product, with the ecosystem's own version order. A finding
says which node carries the software, which repository and file it comes from, which
workloads run what that file builds (through the repository's image repositories on the
map) and whether any of them faces the internet, the version that fixes it, and what
stands in the way of a plain bump: no fix yet, a floating tag, a transitive dependency, a
major upgrade. The policy decides from there; nothing here decides.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from captains.mapper.software import ecr_parts
from captains.patch import versions as _versions
from captains.patch.advisories import Advisory, Affected
from captains.patch.inventory import Inventory, InventoryItem, source_package
from infra_map.models import RelationshipEdge, ResourceNode

DEBIAN_RELEASES = {"10": "buster", "11": "bullseye", "12": "bookworm", "13": "trixie", "14": "forky"}
PACKAGE_TYPES = frozenset({"composer", "npm", "pypi", "golang", "cargo", "gem"})
OS_TYPES = frozenset({"deb", "apk", "rpm"})


@dataclass
class Workload:
    node_id: str
    node_type: str
    name: str
    exposed: bool = False
    via: Optional[str] = None     # how the finding reaches it: "image acme-web" or "" when it is the node itself

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class Finding:
    advisory_id: str
    node_id: str
    node_type: str
    node_name: str
    purl: str
    purl_type: str
    package: str
    version: Optional[str]
    fixed: List[str] = field(default_factory=list)       # every fixed version the advisory names for this package and release
    fixed_version: Optional[str] = None                   # the one a fix would move to
    source: str = ""                                      # the feed that gave the affected entry
    release: Optional[str] = None                         # the distro release, for OS packages
    status: Optional[str] = None                          # the tracker's word, for OS packages
    repo: Optional[str] = None
    path: Optional[str] = None
    evidence: str = ""
    relation: str = ""                                    # image | base | package | direct | transitive | layer | ami | runtime
    workloads: List[Workload] = field(default_factory=list)
    exposed: bool = False
    blockers: List[str] = field(default_factory=list)     # no_fix | floating_tag | transitive | major_upgrade | unknown_version
    note: Optional[str] = None
    cvss: Optional[float] = None
    severity: Optional[str] = None
    kev: bool = False
    epss: Optional[float] = None
    summary: str = ""

    @property
    def id(self) -> str:
        return f"{self.advisory_id}:{self.node_id}:{self.purl}"

    @property
    def key(self) -> str:
        """The row key: short, deterministic, safe in a URL."""
        return hashlib.sha1(self.id.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if k != "workloads"}
        out["workloads"] = [w.to_dict() for w in self.workloads]
        out["id"], out["key"] = self.id, self.key
        return out


# ── the topology: files → images → workloads ─────────────────────────────────

@dataclass
class Topology:
    """What the map says about who runs what a repository builds: the ECR repositories a repository
    publishes to (``publishes_image_to`` edges) and the workloads whose images come from them."""

    repo_images: Dict[str, Set[str]] = field(default_factory=dict)         # repo full name -> ECR repository URIs
    image_workloads: Dict[str, List[Workload]] = field(default_factory=dict)   # ECR repository URI -> workloads running it
    services: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)    # task definition id -> the services running it (node_id, name)
    repo_workflows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)   # repo -> its workflows (name, events, jobs, publishes)

    def workloads_for_repo(self, repo: Optional[str]) -> List[Workload]:
        if not repo:
            return []
        out: Dict[str, Workload] = {}
        for uri in sorted(self.repo_images.get(repo, ())):
            for workload in self.image_workloads.get(uri, []):
                out.setdefault(workload.node_id, Workload(**{**asdict(workload), "via": f"image {uri.rsplit('/', 1)[-1]}"}))
        return list(out.values())

    @classmethod
    def from_graph(cls, nodes: Iterable[ResourceNode], edges: Iterable[RelationshipEdge]) -> "Topology":
        by_id = {n.resource_id: n for n in nodes}
        topology = cls()
        ecr_uri = {n.resource_id: str((n.attributes or {}).get("repositoryUri") or "") for n in by_id.values() if n.type == "ecr_repository"}
        publishing: Set[str] = set()
        for edge in edges:
            if edge.relationship_type == "uses_in":
                a, b = by_id.get(edge.from_resource_id), by_id.get(edge.to_resource_id)
                if a is not None and b is not None and {a.type, b.type} == {"ecs_task_definition", "ecs_service"}:
                    td, svc = (a, b) if a.type == "ecs_task_definition" else (b, a)
                    topology.services.setdefault(td.resource_id, []).append({"node_id": svc.resource_id, "name": svc.name, "exposed": bool((svc.attributes or {}).get("exposed"))})
            if edge.relationship_type != "publishes_image_to":
                continue
            uri = ecr_uri.get(edge.to_resource_id)
            source = by_id.get(edge.from_resource_id)
            repo = (source.attributes or {}).get("repo") if source else None
            if uri and repo:
                topology.repo_images.setdefault(str(repo), set()).add(uri)
                publishing.add(edge.from_resource_id)
        for node in by_id.values():
            if node.type == "github_workflow" and (node.attributes or {}).get("repo"):
                attrs = node.attributes or {}
                topology.repo_workflows.setdefault(str(attrs["repo"]), []).append({"name": node.name, "events": list(attrs.get("events") or []), "jobs": list(attrs.get("jobs") or []),
                                                                                 "publishes": node.resource_id in publishing})
        for node in by_id.values():
            if node.type != "ecs_task_definition":
                continue
            attrs = node.attributes or {}
            for image in attrs.get("images") or []:
                parts = ecr_parts(str(image.get("image") or ""))
                if not parts:
                    continue
                uri = f"{parts[0]}.dkr.ecr.{parts[1]}.amazonaws.com/{parts[2]}"
                topology.image_workloads.setdefault(uri, []).append(Workload(node_id=node.resource_id, node_type=node.type, name=node.name, exposed=bool(attrs.get("exposed"))))
        return topology


# ── the match ─────────────────────────────────────────────────────────────────

def _release_matches(item: InventoryItem, entry: Affected) -> bool:
    """An OS package entry applies when the tracker's release is the item's distro release."""
    distro = item.distro
    if not distro or not entry.release:
        return entry.release is None   # a Dockerfile package with no release known: only unreleased entries apply
    name, release = distro
    if (entry.namespace or "") != name:
        return False
    if release is None:
        return True
    want = entry.release.lower().lstrip("v")
    have = release.lower()
    if name == "debian":
        return want in (have, DEBIAN_RELEASES.get(have, ""), next((k for k, v in DEBIAN_RELEASES.items() if v == have), ""))
    return want == have or want.startswith(have + ".") or have.startswith(want + ".")


def _package_matches(item: InventoryItem, entry: Affected) -> bool:
    item_name = item.name.lower()
    entry_name = entry.name.lower()
    if entry.purl_type in OS_TYPES:
        bare = item_name.rsplit("/", 1)[-1]
        return entry_name in (bare, source_package(bare)) or bare.startswith(entry_name + "-")
    if entry.purl_type == "npm":
        return item_name == entry_name
    return item_name == entry_name or item_name.rsplit("/", 1)[-1] == entry_name.rsplit("/", 1)[-1] and "/" not in entry_name


def _os_affected(item: InventoryItem, entry: Affected) -> Tuple[bool, List[str]]:
    """``(affected, blockers)`` for an OS package against a tracker's or OSV's entry."""
    status = (entry.status or "").lower()
    if status in ("not-affected", "dne", "ignored", "not vulnerable", "end-of-life", "needs-triage") and not entry.fixed and not entry.ranges:
        return False, []
    if entry.ranges or entry.versions:
        if not item.version:
            return True, ["unknown_version"]
        return _versions.affected(entry.purl_type, item.version, entry.ranges, entry.versions), []
    if entry.fixed:
        if not item.version:
            return True, []    # unpinned: whatever the base image's index holds; the fix is the pin
        return any(_versions.compare(entry.purl_type, item.version, f) < 0 for f in entry.fixed), []
    if status in ("needed", "vulnerable", "pending", "deferred", "open", "affected", "released"):
        return True, ["no_fix"] if status != "released" else []
    return False, []


def _product_affected(item: InventoryItem, entry: Affected) -> Tuple[bool, Optional[str], List[str]]:
    """A docker-library image or a Bref layer against a product entry: ``(affected, upstream version, blockers)``."""
    if item.type == "docker":
        version, floating = _versions.tag_version(item.version)
        if version is None:
            return True, None, ["floating_tag", "unknown_version"]
        hit = _versions.affected("docker", version, entry.ranges, entry.versions)
        if floating:
            # a series tag: affected when the series intersects a range; whatever was pulled is unknown
            series_hit = any((r.introduced or "0").startswith(version + ".") or (r.fixed or "").startswith(version + ".") for r in entry.ranges)
            return (hit or series_hit), version, ["floating_tag"] if (hit or series_hit) else []
        return hit, version, []
    if item.type == "generic" and item.name.startswith("bref-layer/"):
        # php-83-fpm runs PHP 8.3.x; which patch release the layer ships is Bref's to say
        layer = item.name.split("/", 1)[1]
        digits = layer.split("-")
        series = next((f"{d[0]}.{d[1:]}" for d in digits if d.isdigit() and len(d) == 2), None)
        if not series or entry.product != "php":
            return False, None, []
        in_series = any((r.introduced or "").startswith(series + ".") or (r.fixed or "").startswith(series + ".") for r in entry.ranges)
        return in_series, series, ["unknown_version"] if in_series else []
    return False, None, []


def match(inventory: Inventory, advisories: Iterable[Advisory], topology: Optional[Topology] = None) -> List[Finding]:
    """Every finding the inventory and the advisories make together."""
    topology = topology or Topology()
    by_name: Dict[Tuple[str, str], List[InventoryItem]] = {}
    products: Dict[str, List[InventoryItem]] = {}
    os_items: Dict[str, List[InventoryItem]] = {}
    for item in inventory.items:
        if item.type in PACKAGE_TYPES:
            by_name.setdefault((item.type, item.name.lower()), []).append(item)
        elif item.type in OS_TYPES:
            os_items.setdefault(item.type, []).append(item)
        elif item.type == "docker" and item.qualifiers.get("repository_url") is None:
            products.setdefault(item.name, []).append(item)
        elif item.type == "generic" and item.name.startswith("bref-layer/"):
            products.setdefault("php", []).append(item)
    findings: Dict[str, Finding] = {}
    for advisory in advisories:
        if advisory.withdrawn:
            continue
        for entry in advisory.affected:
            candidates: List[Tuple[InventoryItem, bool, Optional[str], List[str]]] = []
            if entry.purl_type in PACKAGE_TYPES:
                for item in by_name.get((entry.purl_type, entry.name.lower()), []):
                    if not item.version:
                        continue   # the lockfile beside it has the version
                    hit = _versions.affected(entry.purl_type, item.version, entry.ranges, entry.versions)
                    candidates.append((item, hit, item.version, []))
            elif entry.purl_type in OS_TYPES:
                for item in os_items.get(entry.purl_type, []):
                    if not _package_matches(item, entry) or not _release_matches(item, entry):
                        continue
                    hit, blockers = _os_affected(item, entry)
                    candidates.append((item, hit, item.version, blockers))
            elif entry.product:
                for item in products.get(entry.product, []):
                    hit, version, blockers = _product_affected(item, entry)
                    candidates.append((item, hit, version, blockers))
            for item, hit, version, blockers in candidates:
                if not hit:
                    continue
                finding = _finding(advisory, entry, item, version, blockers, topology)
                existing = findings.get(finding.id)
                if existing is None or (not existing.fixed_version and finding.fixed_version):
                    findings[finding.id] = finding
    return sorted(findings.values(), key=lambda f: (not f.exposed, -(f.cvss or 0), f.advisory_id, f.node_id))


def _finding(advisory: Advisory, entry: Affected, item: InventoryItem, version: Optional[str], blockers: List[str], topology: Topology) -> Finding:
    purl_type = entry.purl_type if entry.purl_type in OS_TYPES or entry.purl_type in PACKAGE_TYPES else item.type
    fixed = list(entry.fixed)
    compare_type = "docker" if entry.product else purl_type
    fixed_version, major_bump = _versions.choose_fix(compare_type, version, fixed)
    blockers = list(blockers)
    if not fixed:
        blockers.append("no_fix")
    elif fixed_version is None and version:
        blockers.append("no_fix")
    if major_bump:
        blockers.append("major_upgrade")
    if item.relation == "transitive":
        blockers.append("transitive")
    if item.relation == "dev":
        blockers.append("dev_only")
    workloads: List[Workload] = []
    if item.node_type in ("ecs_task_definition", "lambda_function", "ec2_instance", "ecs_service"):
        workloads.append(Workload(node_id=item.node_id, node_type=item.node_type, name=item.node_name, exposed=item.exposed))
    else:
        workloads.extend(topology.workloads_for_repo(item.repo))
    exposed = any(w.exposed for w in workloads)
    note = None
    if "floating_tag" in blockers:
        note = f"{item.version or 'latest'} is a floating tag: what runs depends on when the image was pulled"
    elif "unknown_version" in blockers and item.name.startswith("bref-layer/"):
        note = f"Bref layer {item.name.split('/', 1)[1]}:{item.version} runs PHP {version}; the layer that ships {fixed_version or 'the fix'} is Bref's next release"
    elif "no_fix" in blockers:
        note = f"no fixed version yet{f' ({entry.status})' if entry.status else ''}"
    elif "transitive" in blockers:
        note = "a transitive dependency: the bump is on the direct dependency that brings it"
    elif "major_upgrade" in blockers:
        note = f"the only fix, {fixed_version}, is a major upgrade from {version}"
    return Finding(
        advisory_id=advisory.id, node_id=item.node_id, node_type=item.node_type, node_name=item.node_name, purl=item.purl, purl_type=purl_type,
        package=entry.name if entry.purl_type in OS_TYPES or entry.purl_type in PACKAGE_TYPES else item.name, version=version or item.version,
        fixed=sorted(set(fixed)), fixed_version=fixed_version, source=entry.source, release=entry.release, status=entry.status, repo=item.repo, path=item.path,
        evidence=item.evidence, relation=item.relation, workloads=workloads, exposed=exposed, blockers=sorted(set(blockers)), note=note,
        cvss=advisory.cvss, severity=advisory.severity, kev=bool(advisory.kev), epss=(advisory.epss or {}).get("score"), summary=advisory.summary[:200],
    )


def coverage(nodes: Sequence[ResourceNode]) -> Dict[str, Any]:
    """What the Patches tab says about the inventory: how many images are resolved and where the rest are."""
    task_defs = [n for n in nodes if n.type == "ecs_task_definition"]
    images = [img for n in task_defs for img in ((n.attributes or {}).get("images") or [])]
    resolved = sum(1 for img in images if img.get("digest"))
    outside = sum(1 for img in images if not ecr_parts(str(img.get("image") or "")))
    files = sum(1 for n in nodes if n.type in ("github_dockerfile", "github_manifest_file", "github_lockfile", "github_deploy_file") and (n.attributes or {}).get("software"))
    return {"task_definitions": len(task_defs), "images": len(images), "images_resolved": resolved, "images_outside_ecr": outside,
            "lambda_functions": sum(1 for n in nodes if n.type == "lambda_function"), "instances": sum(1 for n in nodes if n.type == "ec2_instance"),
            "software_files": files,
            "line": f"{resolved} of {len(images)} images resolved" + (f"; {outside} task definition image{'s' if outside != 1 else ''} outside ECR" if outside else "")}
