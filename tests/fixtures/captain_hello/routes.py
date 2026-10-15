"""The toy captain's API: one read, one write, under its route prefix."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from compass_api.routes._workspace import get_workspace_config
from compass_core.queue import queue_proxy

router = APIRouter()


@router.get("/api/hello")
def read_hello(request: Request) -> Dict[str, Any]:
    section = get_workspace_config(request).get("hello") or {}
    return {"greeting": section.get("greeting", "hello")}


@router.post("/api/hello/greet")
def greet(request: Request, payload: Dict[str, Any] = {}) -> Dict[str, Any]:
    from compass_api.routes._workspace import get_actor_ids

    return queue_proxy.enqueue(captain="hello", command="greet", payload={**payload, **get_actor_ids(request)})
