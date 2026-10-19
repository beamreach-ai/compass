from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

try:  # pragma: no cover - optional dependency for runtime
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:  # pragma: no cover - optional dependency for runtime
    import hcl2
except ImportError:  # pragma: no cover
    hcl2 = None

try:  # pragma: no cover - optional dependency for runtime
    from github import Github
    from github.GithubException import GithubException
except ImportError:  # pragma: no cover
    Github = None  # type: ignore

    class GithubException(Exception):  # type: ignore
        """Fallback GithubException so type checking continues."""

from compass_core.github import client as client_for_token
from compass_core.github import list_installation_repositories, resolve_github_token
from infra_map.facts import EdgeFact, FactBatch, NodeFact
from infra_map.materializer import InfraMapMaterializer
from infra_map.models import RelationshipEdge, ResourceNode

from .infra_map_ids import (
    github_file_node_id,
    github_repo_node_id,
    repo_scope,
    tf_address_node_id,
    tf_root_module_node_id,
    tf_root_scope,
    tf_state_file_node_id,
)

LOGGER = logging.getLogger("github-infra-mapper")

ARN_REGEX = re.compile(r"(arn:aws:[A-Za-z0-9_-]+:[A-Za-z0-9_-]*:\d{12}:[^\s'\"`]+)")
ECR_URI_REGEX = re.compile(
    r"(?P<uri>\b\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9._/-]+)",
    re.IGNORECASE,
)


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_yaml_load(payload: str) -> Dict:
    if not yaml:
        return {}
    try:
        parsed = yaml.safe_load(payload) or {}
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _simplify_value(value):
    # python-hcl2 >= v4 wraps string literals with their original quotes, e.g. '"foo"' → strip them.
    def _unquote(v):
        if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            return v[1:-1]
        return v

    if isinstance(value, (str, int, float, bool)):
        return _unquote(value) if isinstance(value, str) else value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, (str, int, float, bool)):
            return _unquote(first) if isinstance(first, str) else first
    return None


def _normalize_hcl_keys(value):
    # python-hcl2 >= v4 also wraps block labels (dict keys) with their original
    # quotes, e.g. {'"aws_lambda_function"': ...}. Strip them recursively so
    # resource types, names, and module labels stay valid Terraform addresses.
    def _unquote(key):
        if isinstance(key, str) and len(key) >= 2 and key[0] == '"' and key[-1] == '"':
            return key[1:-1]
        return key

    if isinstance(value, dict):
        return {_unquote(key): _normalize_hcl_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_hcl_keys(item) for item in value]
    return value


def _quote_path(path: str) -> str:
    return quote(path, safe="/._-")


@dataclass
class _GitTreeEntry:
    path: str
    type: str
    sha: str


class AWSResourceCatalog:
    """Helper that allows fast lookups for AWS resources discovered earlier."""

    def __init__(self, nodes: Sequence[ResourceNode]):
        self.by_arn: Dict[str, ResourceNode] = {}
        self.by_type_and_name: Dict[Tuple[str, str], List[ResourceNode]] = {}
        self.ecr_by_uri: Dict[str, ResourceNode] = {}

        for node in nodes:
            rid = node.resource_id
            if isinstance(rid, str) and rid.startswith("arn:"):
                self.by_arn[rid] = node

            safe_name = (node.name or "").lower().strip()
            if safe_name:
                key = (node.type, safe_name)
                self.by_type_and_name.setdefault(key, []).append(node)

            if node.type == "ecr_repository":
                uri = (node.attributes or {}).get("repositoryUri")
                if isinstance(uri, str):
                    normalized = self._normalize_ecr_uri(uri)
                    self.ecr_by_uri[normalized] = node

    @staticmethod
    def _normalize_ecr_uri(uri: str) -> str:
        base = uri.split("@", 1)[0]
        if "/" in base:
            prefix, repo = base.rsplit("/", 1)
            if ":" in repo:
                repo = repo.split(":", 1)[0]
            base = f"{prefix}/{repo}"
        return base

    def find_arn(self, arn: str) -> Optional[ResourceNode]:
        return self.by_arn.get(arn)

    def find_ecr_by_uri(self, uri: str) -> Optional[ResourceNode]:
        normalized = self._normalize_ecr_uri(uri)
        return self.ecr_by_uri.get(normalized)

    def find_by_type_and_name(self, node_type: str, name: str) -> Optional[ResourceNode]:
        key = (node_type, name.lower().strip())
        matches = self.by_type_and_name.get(key, [])
        return matches[0] if matches else None


class GitHubGraphCollector:
    """
    Build graph nodes/edges for GitHub repositories, files, and workflows.
    """

    def __init__(
        self,
        org_id: str,
        github_config: Dict,
        *,
        github_client=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.org_id = org_id
        self.github_config = github_config or {}
        self.allowed_orgs = {
            org.lower()
            for org in self.github_config.get("orgs", [])
            if isinstance(org, str) and org
        }
        if not self.allowed_orgs and isinstance(self.github_config.get("org"), str):
            self.allowed_orgs = {self.github_config["org"].lower()}
        self.skip_archived = _bool(self.github_config.get("skip_archived", True), True)
        self.max_repos = _as_int(self.github_config.get("max_repos"))
        self.max_files_per_repo = _as_int(self.github_config.get("max_files_per_repo"))
        self.logger = logger or LOGGER
        installation_id_raw = self.github_config.get("installation_id")
        self.installation_id: Optional[int] = None
        try:
            if installation_id_raw is not None:
                self.installation_id = int(installation_id_raw)
        except (TypeError, ValueError):
            self.installation_id = None

        if github_client is not None:
            self.github = github_client
        else:
            token = resolve_github_token({"github": self.github_config}, capability="read") or self.github_config.get("token")
            if not token:
                raise ValueError("GitHub token missing in configuration.")
            if Github is None:
                raise RuntimeError("PyGithub must be installed to use GitHubGraphCollector.")
            self.github = client_for_token(token)

        self.nodes: Dict[str, ResourceNode] = {}
        self.edges: Dict[Tuple[str, str, str], RelationshipEdge] = {}
        self.aws_catalog = AWSResourceCatalog([])
        self.materializer = InfraMapMaterializer()

    def collect(self, *, existing_nodes: Sequence[ResourceNode] = ()) -> Tuple[List[ResourceNode], List[RelationshipEdge]]:
        node_map: Dict[str, ResourceNode] = {}
        edge_map: Dict[Tuple[str, str, str], RelationshipEdge] = {}
        for batch in self.collect_fact_batches(existing_nodes=existing_nodes):
            nodes, edges = self.materializer.materialize_batch(batch)
            for node in nodes:
                node_map[node.resource_id] = node
            for edge in edges:
                edge_map[(edge.from_resource_id, edge.to_resource_id, edge.relationship_type)] = edge
        return list(node_map.values()), list(edge_map.values())

    def collect_fact_batches(self, *, existing_nodes: Sequence[ResourceNode] = ()) -> List[FactBatch]:
        self.nodes = {}
        self.edges = {}
        self.aws_catalog = AWSResourceCatalog(existing_nodes or [])
        processed = 0
        for repo in self._iter_repos():
            try:
                self._process_repo(repo)
                processed += 1
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.warning("⚠️ Failed to process repository %s: %s", getattr(repo, "full_name", "?"), exc)
            if self.max_repos and processed >= self.max_repos:
                break
        return self._build_fact_batches()

    # ------------------------------------------------------------------ repo scanning helpers
    def _iter_repos(self) -> Iterable:
        if self.installation_id is not None:
            try:
                payload = list_installation_repositories(self.installation_id)
            except Exception as exc:  # pragma: no cover - network failure
                self.logger.error("❌ Unable to list GitHub installation repositories: %s", exc)
                return []

            count = 0
            repositories = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(repositories, list):
                return []
            for repo_info in repositories:
                if not isinstance(repo_info, dict):
                    continue
                full_name = repo_info.get("full_name")
                owner_info = repo_info.get("owner") if isinstance(repo_info.get("owner"), dict) else {}
                owner = str(owner_info.get("login") or "").lower()
                if self.allowed_orgs and owner not in self.allowed_orgs:
                    continue
                if self.skip_archived and bool(repo_info.get("archived")):
                    continue
                if not full_name:
                    continue
                try:
                    yield self.github.get_repo(str(full_name))
                except GithubException as exc:  # pragma: no cover - network failure
                    self.logger.warning("⚠️ Failed to access repository %s: %s", full_name, exc)
                    continue
                count += 1
                if self.max_repos and count >= self.max_repos:
                    break
            return

        try:
            repos = self.github.get_user().get_repos()
        except GithubException as exc:  # pragma: no cover - network failure
            self.logger.error("❌ Unable to list GitHub repositories: %s", exc)
            return []

        count = 0
        for repo in repos:
            owner = repo.owner.login.lower()
            if self.allowed_orgs and owner not in self.allowed_orgs:
                continue
            if self.skip_archived and getattr(repo, "archived", False):
                continue
            yield repo
            count += 1
            if self.max_repos and count >= self.max_repos:
                break

    def _process_repo(self, repo) -> None:
        repo_node = self._build_repo_node(repo)
        self._add_node(repo_node)

        branch = repo.default_branch or "main"
        tree = self._fetch_tree(repo, branch)
        if not tree:
            return

        files_processed = 0
        terraform_files: Dict[str, str] = {}
        for entry in tree:
            classification = self._classify_file(entry.path)
            if not classification:
                continue
            file_node = self._build_file_node(repo, entry, classification)
            self._add_node(file_node)
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=repo_node.resource_id,
                    to_resource_id=file_node.resource_id,
                    relationship_type="contains",
                    confidence="high",
                    evidence=["github:list_tree"],
                    attributes={"path": entry.path},
                )
            )
            content = self._fetch_file_content(repo, entry.path, branch)
            if content is None:
                continue
            text = content.decode("utf-8", errors="replace")
            self._link_arns(file_node.resource_id, text, relationship_type="references")
            self._link_ecr(text, source_id=file_node.resource_id, relationship_type="publishes_image_to")
            if classification == "terraform":
                terraform_files[entry.path] = text

            if classification == "workflow":
                self._process_workflow(repo, file_node, text, repo_node)
            elif classification == "terraform":
                self._process_terraform_file(repo, file_node, text, repo_node)
            elif classification == "dockerfile":
                self._annotate_dockerfile(file_node, text)

            files_processed += 1
            if self.max_files_per_repo and files_processed >= self.max_files_per_repo:
                break

        if terraform_files:
            self._process_terraform_topology(repo, terraform_files)

    def _fetch_tree(self, repo, branch: str) -> List[_GitTreeEntry]:
        try:
            tree = repo.get_git_tree(branch, recursive=True)
        except GithubException as exc:
            self.logger.warning("⚠️ Could not fetch git tree for %s@%s: %s", repo.full_name, branch, exc)
            return []
        entries: List[_GitTreeEntry] = []
        for item in getattr(tree, "tree", []):
            if getattr(item, "type", "") != "blob":
                continue
            entries.append(
                _GitTreeEntry(
                    path=item.path,
                    type=item.type,
                    sha=item.sha,
                )
            )
        return entries

    def _fetch_file_content(self, repo, path: str, branch: str) -> Optional[bytes]:
        try:
            blob = repo.get_contents(path, ref=branch)
            return getattr(blob, "decoded_content", None)
        except GithubException as exc:
            self.logger.warning("⚠️ Could not fetch %s@%s: %s", path, branch, exc)
            return None

    # ------------------------------------------------------------------ node builders
    def _build_repo_node(self, repo) -> ResourceNode:
        pushed_at: Optional[datetime] = getattr(repo, "pushed_at", None)
        attributes = {
            "url": repo.html_url,
            "default_branch": repo.default_branch,
            "archived": bool(repo.archived),
        }
        if pushed_at:
            attributes["pushed_at"] = pushed_at.isoformat()
        if repo.language:
            attributes["language"] = repo.language
        return ResourceNode(
            org_id=self.org_id,
            resource_id=github_repo_node_id(repo.full_name),
            type="github_repo",
            node_class="container",
            account_id=repo.owner.login,
            region="global",
            name=repo.full_name,
            tags={"visibility": "private" if repo.private else "public"},
            attributes=attributes,
            source="git_scan",
            scope=repo_scope(repo.full_name),
        )

    def _build_file_node(self, repo, entry: _GitTreeEntry, classification: str) -> ResourceNode:
        file_type_map = {
            "workflow": "github_workflow_file",
            "terraform": "github_terraform_file",
            "dockerfile": "github_dockerfile",
        }
        return ResourceNode(
            org_id=self.org_id,
            resource_id=github_file_node_id(repo.full_name, entry.path),
            type=file_type_map[classification],
            node_class="container",
            account_id=repo.owner.login,
            region="global",
            name=f"{repo.name}/{entry.path}",
            attributes={
                "path": entry.path,
                "sha": entry.sha,
                "repo": repo.full_name,
            },
            source="git_scan",
            scope=repo_scope(repo.full_name),
        )

    # ------------------------------------------------------------------ file processors
    def _classify_file(self, path: str) -> Optional[str]:
        lowered = path.lower()
        if lowered.startswith(".github/workflows/") and lowered.endswith((".yml", ".yaml")):
            return "workflow"
        if lowered.endswith(".tf"):
            return "terraform"
        filename = lowered.rsplit("/", 1)[-1]
        if filename == "dockerfile" or filename.startswith("dockerfile."):
            return "dockerfile"
        return None

    def _process_workflow(self, repo, file_node: ResourceNode, text: str, repo_node: ResourceNode) -> None:
        parsed = _safe_yaml_load(text)
        workflow_name = parsed.get("name") or file_node.attributes.get("path")
        slug = quote(str(workflow_name).lower().replace(" ", "-"))
        workflow_node = ResourceNode(
            org_id=self.org_id,
            resource_id=f"{file_node.resource_id}::workflow::{slug}",
            type="github_workflow",
            account_id=repo.owner.login,
            region="global",
            name=str(workflow_name),
            attributes={
                "file": file_node.attributes.get("path"),
                "repo": repo.full_name,
                "events": self._extract_workflow_events(parsed.get("on")),
                "jobs": list((parsed.get("jobs") or {}).keys()),
            },
        )
        self._add_node(workflow_node)
        self._add_edge(
            RelationshipEdge(
                org_id=self.org_id,
                from_resource_id=file_node.resource_id,
                to_resource_id=workflow_node.resource_id,
                relationship_type="defines_workflow",
                confidence="high",
                evidence=["github:workflow_file"],
            )
        )
        self._add_edge(
            RelationshipEdge(
                org_id=self.org_id,
                from_resource_id=repo_node.resource_id,
                to_resource_id=workflow_node.resource_id,
                relationship_type="owns_workflow",
                confidence="high",
                evidence=["github:workflow_file"],
            )
        )
        self._link_arns(workflow_node.resource_id, text, relationship_type="references")
        self._link_ecr(text, source_id=workflow_node.resource_id, relationship_type="publishes_image_to")

    def _extract_workflow_events(self, on_field) -> List[str]:
        if isinstance(on_field, str):
            return [on_field]
        if isinstance(on_field, list):
            return [str(item) for item in on_field]
        if isinstance(on_field, dict):
            return list(on_field.keys())
        return []

    def _annotate_dockerfile(self, file_node: ResourceNode, text: str) -> None:
        stages: List[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.lower().startswith("from "):
                parts = stripped.split()
                if len(parts) >= 2:
                    stages.append(parts[1])
        file_node.attributes["base_images"] = stages

    def _process_terraform_file(self, repo, file_node: ResourceNode, text: str, repo_node: ResourceNode) -> None:
        modules = self._extract_terraform_modules(text)
        for module in modules:
            module_name, attrs = module
            module_node = ResourceNode(
                org_id=self.org_id,
                resource_id=f"{file_node.resource_id}::module::{quote(module_name)}",
                type="terraform_module",
                account_id=repo.owner.login,
                region="global",
                name=module_name,
                attributes={
                    "path": file_node.attributes.get("path"),
                    "repo": repo.full_name,
                    "source": _simplify_value(attrs.get("source")),
                    "version": _simplify_value(attrs.get("version")),
                },
                source="git_scan",
                scope=repo_scope(repo.full_name),
            )
            self._add_node(module_node)
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=file_node.resource_id,
                    to_resource_id=module_node.resource_id,
                    relationship_type="defines_module",
                    confidence="high",
                    evidence=["github:terraform_file"],
                )
            )
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=repo_node.resource_id,
                    to_resource_id=module_node.resource_id,
                    relationship_type="contains_module",
                    confidence="medium",
                    evidence=["github:terraform_file"],
                )
            )

        ecr_targets = self._extract_terraform_ecr_targets(text)
        for repo_name in ecr_targets:
            aws_node = self.aws_catalog.find_by_type_and_name("ecr_repository", repo_name)
            if not aws_node:
                continue
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=file_node.resource_id,
                    to_resource_id=aws_node.resource_id,
                    relationship_type="defines_resource",
                    confidence="medium",
                    evidence=["github:terraform_file"],
                    attributes={"resource": "aws_ecr_repository", "name": repo_name},
                    source="git_scan",
                    scope=repo_scope(repo.full_name),
                )
            )

    def _process_terraform_topology(self, repo, terraform_files: Dict[str, str]) -> None:
        parsed_by_path: Dict[str, Dict] = {}
        files_by_dir: Dict[str, List[str]] = {}
        for path, text in terraform_files.items():
            parsed = self._parse_hcl(text)
            parsed_by_path[path] = parsed
            directory = posixpath.dirname(path) or "."
            files_by_dir.setdefault(directory, []).append(path)

        root_dirs = []
        for directory, paths in files_by_dir.items():
            backend = self._extract_s3_backend(paths, parsed_by_path)
            if backend:
                root_dirs.append((directory, backend))

        for directory, backend in root_dirs:
            root_id = tf_root_module_node_id(repo.full_name, directory)
            root_scope_id = tf_root_scope(root_id)
            root_node = ResourceNode(
                org_id=self.org_id,
                resource_id=root_id,
                type="tf_root_module",
                node_class="container",
                account_id=repo.owner.login,
                region="global",
                name=directory,
                attributes={
                    "path": directory,
                    "repo": repo.full_name,
                    "backend": backend,
                },
                source="git_scan",
                scope=root_scope_id,
            )
            self._add_node(root_node)

            bucket = backend.get("bucket")
            key = backend.get("key")
            if isinstance(bucket, str) and isinstance(key, str) and bucket and key:
                self._add_edge(
                    RelationshipEdge(
                        org_id=self.org_id,
                        from_resource_id=root_id,
                        to_resource_id=tf_state_file_node_id(bucket, key),
                        relationship_type="writes",
                        confidence="high",
                        evidence=["github:terraform_backend"],
                        attributes={"backend": "s3"},
                        source="git_scan",
                        scope=root_scope_id,
                    )
                )

            self._walk_root_module(
                repo_full_name=repo.full_name,
                tf_root_module_id=root_id,
                current_dir=directory,
                files_by_dir=files_by_dir,
                parsed_by_path=parsed_by_path,
                address_prefix="",
                stack=set(),
            )

    def _extract_terraform_modules(self, text: str) -> List[Tuple[str, Dict]]:
        if not hcl2:
            return []
        try:
            parsed = _normalize_hcl_keys(hcl2.loads(text))
        except Exception:
            return []
        modules: List[Tuple[str, Dict]] = []
        for entry in parsed.get("module", []):
            if not isinstance(entry, dict):
                continue
            for module_name, attrs in entry.items():
                if isinstance(attrs, dict):
                    modules.append((module_name, attrs))
        return modules

    def _extract_terraform_ecr_targets(self, text: str) -> List[str]:
        if not hcl2:
            return []
        try:
            parsed = _normalize_hcl_keys(hcl2.loads(text))
        except Exception:
            return []
        names: List[str] = []
        for entry in parsed.get("resource", []):
            if not isinstance(entry, dict):
                continue
            for resource_type, resources in entry.items():
                if resource_type != "aws_ecr_repository":
                    continue
                for _, attrs in resources.items():
                    if not isinstance(attrs, dict):
                        continue
                    value = _simplify_value(attrs.get("name"))
                    if isinstance(value, str):
                        names.append(value)
        return names

    def _parse_hcl(self, text: str) -> Dict:
        if not hcl2:
            return {}
        try:
            parsed = _normalize_hcl_keys(hcl2.loads(text))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_s3_backend(self, paths: Sequence[str], parsed_by_path: Dict[str, Dict]) -> Optional[Dict]:
        for path in paths:
            parsed = parsed_by_path.get(path) or {}
            for terraform_block in parsed.get("terraform", []):
                if not isinstance(terraform_block, dict):
                    continue
                for backend_block in terraform_block.get("backend", []):
                    if not isinstance(backend_block, dict):
                        continue
                    # python-hcl2 >= v4 encodes block labels with surrounding quotes
                    s3_cfg = backend_block.get("s3") or backend_block.get('"s3"')
                    if not isinstance(s3_cfg, dict):
                        continue
                    bucket = _simplify_value(s3_cfg.get("bucket"))
                    key = _simplify_value(s3_cfg.get("key"))
                    region = _simplify_value(s3_cfg.get("region"))
                    if isinstance(bucket, str) and isinstance(key, str):
                        payload = {"type": "s3", "bucket": bucket, "key": key}
                        if isinstance(region, str):
                            payload["region"] = region
                        return payload
        return None

    def _walk_root_module(
        self,
        *,
        repo_full_name: str,
        tf_root_module_id: str,
        current_dir: str,
        files_by_dir: Dict[str, List[str]],
        parsed_by_path: Dict[str, Dict],
        address_prefix: str,
        stack: set[Tuple[str, str]],
    ) -> None:
        stack_key = (current_dir, address_prefix)
        if stack_key in stack:
            return
        stack.add(stack_key)

        for path in sorted(files_by_dir.get(current_dir, [])):
            file_node_id = github_file_node_id(repo_full_name, path)
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=file_node_id,
                    to_resource_id=tf_root_module_id,
                    relationship_type="part_of",
                    confidence="high",
                    evidence=["github:terraform_file"],
                    attributes={"root_path": current_dir},
                    source="git_scan",
                    scope=tf_root_scope(tf_root_module_id),
                )
            )

            parsed = parsed_by_path.get(path) or {}
            for resource_block in parsed.get("resource", []):
                if not isinstance(resource_block, dict):
                    continue
                for resource_type, resources in resource_block.items():
                    if not isinstance(resources, dict):
                        continue
                    for resource_name in resources.keys():
                        terraform_address = f"{address_prefix}{resource_type}.{resource_name}"
                        address_id = tf_address_node_id(tf_root_module_id, terraform_address)
                        self._add_node(
                            ResourceNode(
                                org_id=self.org_id,
                                resource_id=address_id,
                                type="tf_address",
                                node_class="declaration",
                                account_id="",
                                region="global",
                                name=terraform_address,
                                attributes={
                                    "terraform_address": terraform_address,
                                    "tf_root_module_id": tf_root_module_id,
                                    "repo": repo_full_name,
                                    "path": path,
                                },
                                source="git_scan",
                                scope=tf_root_scope(tf_root_module_id),
                            )
                        )
                        self._add_edge(
                            RelationshipEdge(
                                org_id=self.org_id,
                                from_resource_id=file_node_id,
                                to_resource_id=address_id,
                                relationship_type="declares",
                                confidence="high",
                                evidence=["github:terraform_file"],
                                attributes={"path": path},
                                source="git_scan",
                                scope=tf_root_scope(tf_root_module_id),
                            )
                        )

            for module_block in parsed.get("module", []):
                if not isinstance(module_block, dict):
                    continue
                for module_name, attrs in module_block.items():
                    if not isinstance(attrs, dict):
                        continue
                    source = _simplify_value(attrs.get("source"))
                    if not isinstance(source, str) or not source.startswith(("./", "../")):
                        continue
                    target_dir = posixpath.normpath(posixpath.join(current_dir, source))
                    if target_dir.startswith("../"):
                        continue
                    self._walk_root_module(
                        repo_full_name=repo_full_name,
                        tf_root_module_id=tf_root_module_id,
                        current_dir=target_dir,
                        files_by_dir=files_by_dir,
                        parsed_by_path=parsed_by_path,
                        address_prefix=f"{address_prefix}module.{module_name}.",
                        stack=stack,
                    )

    # ------------------------------------------------------------------ linking helpers
    def _link_arns(self, source_id: str, text: str, *, relationship_type: str) -> None:
        for match in ARN_REGEX.finditer(text):
            arn = match.group(1)
            target = self.aws_catalog.find_arn(arn)
            if not target:
                continue
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=source_id,
                    to_resource_id=target.resource_id,
                    relationship_type=relationship_type,
                    confidence="low",
                    evidence=["github:file_match"],
                    attributes={"pattern": "arn", "value": arn},
                )
            )

    def _link_ecr(self, text: str, *, source_id: str, relationship_type: str) -> None:
        for match in ECR_URI_REGEX.finditer(text):
            uri = match.group("uri")
            target = self.aws_catalog.find_ecr_by_uri(uri)
            if not target:
                continue
            self._add_edge(
                RelationshipEdge(
                    org_id=self.org_id,
                    from_resource_id=source_id,
                    to_resource_id=target.resource_id,
                    relationship_type=relationship_type,
                    confidence="medium",
                    evidence=["github:file_match"],
                    attributes={"pattern": "ecr_uri", "value": uri},
                )
            )

    # ------------------------------------------------------------------ storage helpers
    def _add_node(self, node: ResourceNode) -> None:
        if not node.source:
            node.source = "git_scan"
        if not node.scope:
            repo_value = (node.attributes or {}).get("repo")
            node.scope = repo_scope(repo_value or node.account_id)
        self.nodes[node.resource_id] = node

    def _add_edge(self, edge: RelationshipEdge) -> None:
        if not edge.source:
            edge.source = "git_scan"
        if not edge.scope:
            parent = self.nodes.get(edge.from_resource_id)
            if parent and parent.scope:
                edge.scope = parent.scope
        key = (edge.from_resource_id, edge.to_resource_id, edge.relationship_type)
        self.edges[key] = edge

    def _build_fact_batches(self) -> List[FactBatch]:
        scopes: Dict[Tuple[str, str], FactBatch] = {}
        for node in self.nodes.values():
            source = node.source or "git_scan"
            scope = node.scope or repo_scope((node.attributes or {}).get("repo") or node.account_id)
            batch = scopes.setdefault(
                (source, scope),
                FactBatch(org_id=self.org_id, source=source, scope=scope),
            )
            batch.node_facts.append(self._node_to_fact(node))
        for edge in self.edges.values():
            source = edge.source or "git_scan"
            scope = edge.scope or repo_scope(self._repo_from_edge(edge))
            batch = scopes.setdefault(
                (source, scope),
                FactBatch(org_id=self.org_id, source=source, scope=scope),
            )
            batch.edge_facts.append(self._edge_to_fact(edge))
        return list(scopes.values())

    def _repo_from_edge(self, edge: RelationshipEdge) -> str:
        from_node = self.nodes.get(edge.from_resource_id)
        if from_node:
            repo = (from_node.attributes or {}).get("repo")
            if isinstance(repo, str) and repo.strip():
                return repo
        return ""

    def _node_to_fact(self, node: ResourceNode) -> NodeFact:
        return NodeFact(
            org_id=node.org_id,
            entity_id=node.resource_id,
            node_type=node.type,
            account_id=node.account_id,
            region=node.region,
            name=node.name,
            tags=dict(node.tags),
            attributes=dict(node.attributes),
            node_class=node.node_class,
            canonical_ref=node.canonical_ref,
            aliases=list(node.aliases),
            confidence=node.confidence,
            observed_at=node.last_seen,
        )

    def _edge_to_fact(self, edge: RelationshipEdge) -> EdgeFact:
        return EdgeFact(
            org_id=edge.org_id,
            from_entity_id=edge.from_resource_id,
            to_entity_id=edge.to_resource_id,
            edge_type=edge.relationship_type,
            confidence=edge.confidence,
            evidence=list(edge.evidence),
            attributes=dict(edge.attributes),
            observed_at=edge.last_seen,
        )
