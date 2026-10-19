import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from captains.mapper.ownership_resolver import InfraMapOwnershipResolver
from compass_api.routes._infra_map import resolve_infra_map_context
from compass_api.routes._infra_scan import enqueue_full_infra_scan, mark_infra_scan_pending_for_request
from compass_api.routes._workspace import get_actor_ids
from compass_core.queue import queue_proxy
from infra_map.graph_store import GraphStore
from infra_map.models import RelationshipEdge, ResourceNode

router = APIRouter()
logger = logging.getLogger("maps-api")
_iac_queue_client = queue_proxy


class MapReindexRequest(BaseModel):
    mode: str = Field(default="full", description="Mapper execution mode. Only 'full' is currently supported.")
    scope_type: Optional[str] = Field(default=None, description="Future partial remap scope type.")
    scope_value: Optional[str] = Field(default=None, description="Future partial remap scope value.")
    org_id: Optional[str] = None


class MapReindexResponse(BaseModel):
    status: str
    job_id: str
    captain: str = "mapper"
    command: str = "run"
    mode: str = "full"
    existing: bool = False

def _serialize_node(node: ResourceNode) -> Dict[str, Any]:
    data = node.to_item()
    data.pop("record_type", None)
    return data


def _serialize_edge(edge: RelationshipEdge) -> Dict[str, Any]:
    data = edge.to_attributes()
    data.pop("record_type", None)
    data["resource_id"] = edge.canonical_edge_id()
    return data


def _parse_relationship_types(raw: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not raw:
        return None
    values: List[str] = []
    for entry in raw:
        if not entry:
            continue
        for token in str(entry).split(","):
            normalized = token.strip()
            if normalized:
                values.append(normalized)
    return values or None

@router.get("/api/maps")
def get_infra_map(
    request: Request,
    org_id: Optional[str] = Query(None, description="Org/tenant identifier"),
    focus: Optional[str] = Query(None, description="Resource ID to center the subgraph"),
    depth: int = Query(2, ge=1, le=6, description="Traversal depth when focus is provided"),
    direction: str = Query("both", pattern="^(incoming|outgoing|both)$"),
    limit: int = Query(2000, ge=100, le=10000, description="Max items to read when returning the full graph"),
    relationship_types: Optional[List[str]] = Query(
        None, description="Optional list of relationship types to include (comma separated)"
    ),
):
    ctx = resolve_infra_map_context(request)
    map_store: GraphStore = ctx["store"]
    default_org_id: str = ctx["default_org"]
    chosen_org = (org_id or default_org_id).strip()
    if not chosen_org:
        raise HTTPException(status_code=400, detail="org_id is required")

    rel_types = _parse_relationship_types(relationship_types)

    try:
        if focus:
            graph = map_store.neighborhood(chosen_org, focus, depth=depth, direction=direction, types=rel_types, max_nodes=limit).as_dict()
        else:
            graph = map_store.fetch_graph(
                chosen_org,
                limit=limit,
            )
            graph["focus"] = None
    except Exception as exc:
        logger.exception("Failed to load infra map graph")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "workspace_id": ctx["workspace_id"],
        "org_id": chosen_org,
        "focus": graph.get("focus"),
        "nodes": [_serialize_node(node) for node in graph.get("nodes", [])],
        "edges": [_serialize_edge(edge) for edge in graph.get("edges", [])],
    }


@router.get("/api/maps/ownership")
def resolve_infra_ownership(
    request: Request,
    resource_id: str = Query(..., description="Canonical AWS resource identifier"),
    org_id: Optional[str] = Query(None, description="Org/tenant identifier"),
    max_candidates: int = Query(3, ge=1, le=10),
):
    ctx = resolve_infra_map_context(request)
    chosen_org = (org_id or ctx["default_org"]).strip()
    if not chosen_org:
        raise HTTPException(status_code=400, detail="org_id is required")

    try:
        result = InfraMapOwnershipResolver(ctx["store"]).resolve_ownership(
            chosen_org,
            resource_id,
            max_candidates=max_candidates,
        )
    except Exception as exc:
        logger.exception("Failed to resolve infra ownership")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.post("/api/maps/reindex", response_model=MapReindexResponse)
def enqueue_map_reindex(request: Request, body: Optional[MapReindexRequest] = None) -> MapReindexResponse:
    payload = body or MapReindexRequest()
    mode = (payload.mode or "full").strip().lower()
    if mode != "full" or payload.scope_type or payload.scope_value:
        raise HTTPException(status_code=501, detail="Partial mapper remap is not implemented yet")
    mark_infra_scan_pending_for_request(request, reason="manual_mapper_reindex")
    job = enqueue_full_infra_scan(request, trigger="manual_reindex", org_id_override=payload.org_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Connect AWS and GitHub before running the full infra scan")
    return MapReindexResponse(
        status=str(job.get("status") or "queued"),
        job_id=str(job["job_id"]),
        mode=mode,
        existing=bool(job.get("existing")),
    )


# ---------------------------------------------------------------------------
# IAC coverage endpoint
# ---------------------------------------------------------------------------

def _iac_coverage_summary(org_id: str, store: GraphStore) -> Dict[str, Any]:
    graph = store.fetch_graph(org_id)
    aws_nodes = [n for n in graph.get("nodes", []) if n.source == "aws_scan"]

    covered: List[Dict[str, Any]] = []
    unmanaged: List[Dict[str, Any]] = []
    drifted: List[Dict[str, Any]] = []

    by_type: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "covered": 0, "drifted": 0})

    for node in aws_nodes:
        attrs = node.attributes or {}
        score = attrs.get("coverage_score", 0.0)
        is_covered = score >= 1.0
        drift_status = attrs.get("drift_status")
        drift_fields = attrs.get("drift_fields", [])

        summary = {
            "resource_id": node.resource_id,
            "resource_type": node.type,
            "name": node.name,
            "region": node.region,
            "account_id": node.account_id,
        }

        by_type[node.type]["total"] += 1

        if is_covered:
            by_type[node.type]["covered"] += 1
            covered.append(summary)
            if drift_status == "drifted":
                by_type[node.type]["drifted"] += 1
                drifted.append({**summary, "drift_fields": drift_fields})
        else:
            unmanaged.append(summary)

    total = len(aws_nodes)
    covered_count = len(covered)
    coverage_pct = round(covered_count / total * 100, 1) if total > 0 else 0.0

    return {
        "coverage_pct": coverage_pct,
        "total": total,
        "covered": covered_count,
        "unmanaged": len(unmanaged),
        "drifted": len(drifted),
        "by_type": [
            {"type": t, **counts}
            for t, counts in sorted(by_type.items(), key=lambda x: x[1]["total"], reverse=True)
        ],
        "unmanaged_resources": unmanaged,
        "drifted_resources": drifted,
    }


@router.get("/api/iac/coverage")
def get_iac_coverage(
    request: Request,
    org_id: Optional[str] = Query(None),
):
    ctx = resolve_infra_map_context(request)
    chosen_org = (org_id or ctx["default_org"]).strip()
    if not chosen_org:
        raise HTTPException(status_code=400, detail="org_id is required")
    try:
        return _iac_coverage_summary(chosen_org, ctx["store"])
    except Exception as exc:
        logger.exception("Failed to compute IAC coverage")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# IAC import — enqueues batch Terraform import via PR to the iac captain
# ---------------------------------------------------------------------------

class IACImportRequest(BaseModel):
    resource_ids: List[str]
    repo: str
    branch: str = "main"
    module_path: Optional[str] = None
    is_new_module: bool = False
    org_id: Optional[str] = None


class IACImportResponse(BaseModel):
    status: str
    job_id: str
    captain: str = "iac"
    command: str = "import"
    resource_count: int


@router.post("/api/iac/import", response_model=IACImportResponse)
def iac_import(request: Request, body: IACImportRequest) -> IACImportResponse:
    if not body.resource_ids:
        raise HTTPException(status_code=400, detail="resource_ids must not be empty")
    if not body.repo:
        raise HTTPException(status_code=400, detail="repo is required")

    ctx = resolve_infra_map_context(request)
    actor_ids = get_actor_ids(request)

    try:
        job = _iac_queue_client.enqueue(
            captain="iac",
            command="import",
            payload={
                "resource_ids": body.resource_ids,
                "repo": body.repo,
                "branch": body.branch,
                "module_path": body.module_path,
                "is_new_module": body.is_new_module,
                "workspace_id": ctx["workspace_id"],
                "org_id": body.org_id or ctx["default_org"],
                "user_id": actor_ids.get("user_id"),
            },
        )
    except Exception as exc:
        logger.exception("Failed to enqueue IAC import job")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return IACImportResponse(
        status=str(job.get("status") or "queued"),
        job_id=str(job["job_id"]),
        resource_count=len(body.resource_ids),
    )


@router.get("/api/iac/suggestions")
def get_iac_suggestions(
    request: Request,
    org_id: Optional[str] = Query(None),
):
    """Return repos and module paths discovered in the infra map for use in the import form."""
    ctx = resolve_infra_map_context(request)
    chosen_org = (org_id or ctx["default_org"]).strip()
    if not chosen_org:
        raise HTTPException(status_code=400, detail="org_id is required")

    try:
        graph = ctx["store"].fetch_graph(chosen_org)
    except Exception as exc:
        logger.exception("Failed to load infra map for suggestions")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    repos: List[str] = []
    modules: List[Dict[str, str]] = []

    for node in graph.get("nodes", []):
        attrs = node.attributes or {}
        if node.type == "github_repo" and node.name:
            repos.append(node.name)
        elif node.type == "tf_root_module":
            repo = attrs.get("repo") or ""
            path = attrs.get("path") or node.name or ""
            if repo and path:
                modules.append({"repo": repo, "path": path})

    return {
        "repos": sorted(set(repos)),
        "modules": sorted(modules, key=lambda m: (m["repo"], m["path"])),
    }


class IACFixDriftRequest(BaseModel):
    resource_ids: List[str]
    org_id: Optional[str] = None


class IACFixDriftResponse(BaseModel):
    status: str
    job_id: str
    captain: str = "iac"
    command: str = "fix_drift"
    resource_count: int


@router.post("/api/iac/fix-drift", response_model=IACFixDriftResponse)
def iac_fix_drift(request: Request, body: IACFixDriftRequest) -> IACFixDriftResponse:
    if not body.resource_ids:
        raise HTTPException(status_code=400, detail="resource_ids must not be empty")

    ctx = resolve_infra_map_context(request)
    actor_ids = get_actor_ids(request)

    try:
        job = _iac_queue_client.enqueue(
            captain="iac",
            command="fix_drift",
            payload={
                "resource_ids": body.resource_ids,
                "workspace_id": ctx["workspace_id"],
                "org_id": body.org_id or ctx["default_org"],
                "user_id": actor_ids.get("user_id"),
            },
        )
    except Exception as exc:
        logger.exception("Failed to enqueue IAC fix-drift job")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return IACFixDriftResponse(
        status=str(job.get("status") or "queued"),
        job_id=str(job["job_id"]),
        resource_count=len(body.resource_ids),
    )


@router.get("/api/iac/prs")
def list_iac_prs(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
):
    """Return all PRs created by IAC captain jobs (import + fix_drift). See /api/prs for every captain."""
    from compass_api.routes.prs import collect_prs

    actor_ids = get_actor_ids(request)
    workspace_id = actor_ids.get("workspace_id")
    jobs = _iac_queue_client.list_jobs(limit=limit, workspace_id=workspace_id)
    return collect_prs(jobs, captains={"iac"})


@router.post("/api/iac/scan")
def enqueue_iac_scan(request: Request, body: Optional[MapReindexRequest] = None) -> MapReindexResponse:
    """Trigger a full infra re-scan, which recomputes coverage and drift."""
    payload = body or MapReindexRequest()
    mark_infra_scan_pending_for_request(request, reason="iac_scan")
    job = enqueue_full_infra_scan(request, trigger="iac_scan", org_id_override=payload.org_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Connect AWS and GitHub before running the IAC scan")
    return MapReindexResponse(
        status=str(job.get("status") or "queued"),
        job_id=str(job["job_id"]),
        mode="full",
        existing=bool(job.get("existing")),
    )
