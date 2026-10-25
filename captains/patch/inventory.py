"""The inventory: every piece of software on the map, as the mapper recorded it
(``attributes.software`` on workloads and repository files, ``captains/mapper/software.py``),
with the node it sits on and whether that workload faces the internet. The feeds query by
it (OSV by purl, the distro trackers by release, cvelistV5 by product) and the match runs
over it (docs/specs/patching-captain.md §3.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from infra_map.models import ResourceNode

OSV_ECOSYSTEMS = {"composer": "Packagist", "npm": "npm", "pypi": "PyPI", "golang": "Go", "cargo": "crates.io", "gem": "RubyGems"}
DISTRO_ECOSYSTEMS = {"ubuntu": "Ubuntu", "debian": "Debian", "alpine": "Alpine"}
WORKLOAD_TYPES = ("ecs_task_definition", "lambda_function", "ec2_instance")
FILE_TYPES = ("github_dockerfile", "github_manifest_file", "github_lockfile", "github_deploy_file", "github_terraform_file")


@dataclass
class InventoryItem:
    node_id: str
    node_type: str
    node_name: str
    purl: str
    type: str
    name: str
    version: Optional[str]
    relation: str
    evidence: str
    qualifiers: Dict[str, str] = field(default_factory=dict)
    exposed: bool = False
    repo: Optional[str] = None
    path: Optional[str] = None

    @property
    def bare_purl(self) -> str:
        """The purl without qualifiers: what OSV is asked about."""
        return self.purl.split("?", 1)[0]

    @property
    def distro(self) -> Optional[Tuple[str, Optional[str]]]:
        """``(distro, release)`` of an OS package: ``("ubuntu", "24.04")``, ``("alpine", "3.20")``."""
        text = self.qualifiers.get("distro")
        if not text:
            return None
        name, _, release = text.partition("-")
        return name, release or None

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "node_type": self.node_type, "node_name": self.node_name, "purl": self.purl, "type": self.type, "name": self.name,
                "version": self.version, "relation": self.relation, "evidence": self.evidence, "exposed": self.exposed, "repo": self.repo, "path": self.path}


@dataclass
class Inventory:
    items: List[InventoryItem] = field(default_factory=list)
    nodes_read: int = 0
    nodes_with_software: int = 0

    def osv_purls(self) -> List[str]:
        """Versioned package purls in the ecosystems OSV answers for, plus pinned distro packages,
        by their binary name and by the source package OSV keys them under (``php8.3-fpm`` → ``php8.3``)."""
        out: Set[str] = set()
        for item in self.items:
            if not item.version:
                continue
            if item.type in OSV_ECOSYSTEMS:
                out.add(item.bare_purl)
            elif item.type in ("deb", "apk") and item.distro and item.distro[0] in DISTRO_ECOSYSTEMS:
                out.add(item.bare_purl)
                source = source_package(item.name.rsplit("/", 1)[-1])
                if source != item.name.rsplit("/", 1)[-1]:
                    out.add(item.bare_purl.replace(f"/{item.name.rsplit('/', 1)[-1]}@", f"/{source}@"))
        return sorted(out)

    def osv_ecosystems(self) -> List[str]:
        out: Set[str] = set()
        for item in self.items:
            if item.type in OSV_ECOSYSTEMS:
                out.add(OSV_ECOSYSTEMS[item.type])
            elif item.type in ("deb", "apk") and item.distro and item.distro[0] in DISTRO_ECOSYSTEMS:
                out.add(DISTRO_ECOSYSTEMS[item.distro[0]])
        return sorted(out)

    def distro_releases(self) -> Dict[str, Set[str]]:
        """``{"ubuntu": {"24.04"}, "alpine": {"3.20"}, "debian": {"bookworm"}}`` from the OS packages and base images."""
        out: Dict[str, Set[str]] = {}
        for item in self.items:
            if item.type in ("deb", "apk", "rpm") and item.distro:
                name, release = item.distro
                out.setdefault(name, set())
                if release:
                    out[name].add(release)
        return out

    def products(self) -> Dict[str, Set[str]]:
        """``{"php": {"8.3.30"}, "node": {"20.17.0"}}``: the docker-library images in use with the
        upstream version their tag names, and the Lambda runtimes."""
        out: Dict[str, Set[str]] = {}
        for item in self.items:
            if item.type == "docker" and item.qualifiers.get("repository_url") is None:
                version = _upstream_version(item.version)
                out.setdefault(item.name, set())
                if version:
                    out[item.name].add(version)
            elif item.type == "generic" and item.name == "aws-lambda-runtime" and item.version:
                out.setdefault("aws-lambda-runtime", set()).add(item.version)
        return out

    def npm_packages(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for item in self.items:
            if item.type == "npm" and item.version:
                out.setdefault(item.name, set()).add(item.version)
        return out

    def by_purl(self) -> Dict[str, List[InventoryItem]]:
        out: Dict[str, List[InventoryItem]] = {}
        for item in self.items:
            out.setdefault(item.bare_purl, []).append(item)
        return out

    def summary(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for item in self.items:
            by_type[item.type] = by_type.get(item.type, 0) + 1
        return {"nodes_read": self.nodes_read, "nodes_with_software": self.nodes_with_software, "entries": len(self.items), "by_type": by_type,
                "exposed_workloads": len({i.node_id for i in self.items if i.exposed and i.node_type in WORKLOAD_TYPES}),
                "osv_purls": len(self.osv_purls()), "products": {k: sorted(v) for k, v in self.products().items()},
                "distro_releases": {k: sorted(v) for k, v in self.distro_releases().items()}}


BINARY_SUFFIXES = ("-fpm", "-cli", "-cgi", "-dev", "-common", "-doc", "-bin", "-utils", "-tools", "-data", "-core", "-libs", "-devel", "-mysql", "-pgsql",
                   "-curl", "-gd", "-mbstring", "-xml", "-zip", "-intl", "-opcache", "-readline", "-bcmath", "-soap", "-apache2", "-full", "-light", "-extras")


def source_package(binary: str) -> str:
    """The source package a distro binary most likely comes from: one well-known suffix dropped."""
    for suffix in BINARY_SUFFIXES:
        if binary.endswith(suffix) and len(binary) > len(suffix):
            return binary[: -len(suffix)]
    return binary


def _upstream_version(tag: Optional[str]) -> Optional[str]:
    """``8.3.30-fpm-alpine3.20`` → ``8.3.30``; ``20-bookworm`` → ``20``; ``latest`` → None."""
    if not tag:
        return None
    head = tag.split("-", 1)[0]
    return head if head and head[0].isdigit() else None


def collect(nodes: Iterable[ResourceNode]) -> Inventory:
    inventory = Inventory()
    for node in nodes:
        inventory.nodes_read += 1
        attributes = node.attributes or {}
        rows = attributes.get("software") or []
        if not rows:
            continue
        inventory.nodes_with_software += 1
        exposed = bool(attributes.get("exposed"))
        for row in rows:
            if not isinstance(row, dict) or not row.get("purl"):
                continue
            inventory.items.append(InventoryItem(
                node_id=node.resource_id, node_type=node.type, node_name=node.name, purl=str(row["purl"]), type=str(row.get("type") or ""),
                name=str(row.get("name") or ""), version=row.get("version"), relation=str(row.get("relation") or ""), evidence=str(row.get("evidence") or ""),
                qualifiers={k: str(v) for k, v in (row.get("qualifiers") or {}).items()}, exposed=exposed,
                repo=attributes.get("repo"), path=attributes.get("path"),
            ))
    return inventory


def load(config: Dict[str, Any], *, workspace_id: str, org_id: Optional[str] = None) -> Inventory:
    """The inventory of the workspace's map."""
    from infra_map.graph_store import open_store

    org = org_id or str(config.get("org_id") or (config.get("dynamodb_config") or {}).get("org_id") or workspace_id)
    store = open_store(config, workspace_id=workspace_id)
    try:
        graph = store.fetch_graph(org)
    finally:
        store.close()
    return collect(graph.get("nodes") or [])
