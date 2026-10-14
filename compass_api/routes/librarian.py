import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from captains.librarian.service import InfraMapLibrarian
from compass_api.routes._infra_map import resolve_infra_map_context

router = APIRouter()
logger = logging.getLogger("librarian-api")


@router.get("/api/librarian/terraform-location")
def get_terraform_location(
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
        result = InfraMapLibrarian(ctx["store"]).get_terraform_location(
            chosen_org,
            resource_id,
            max_candidates=max_candidates,
        )
    except Exception as exc:
        logger.exception("Failed to resolve Terraform location")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result["workspace_id"] = ctx["workspace_id"]
    result["org_id"] = chosen_org
    return result
