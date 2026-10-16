"""The open API app: ``compass api`` serves it (uvicorn factory ``compass_api.app:create_app``).

One workspace, its config a file (``COMPASS_CONFIG``); username/password
sessions (``APP_USERNAME``/``APP_PASSWORD``) and API tokens; jobs the dashboard
starts are written to the jobs table and run by ``compass serve``. The
dashboard shows the open tabs: the closed ones are switched off with
``DISABLED_FEATURES`` unless you set it yourself.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger("compass.api")

# The dashboard's tabs for features the open edition does not have. (The Queue tab
# stays: it is the jobs list — scans, plants, imports, PR syncs.)
OPEN_DISABLED_FEATURES = "security,finops,compliance,policies"


def routers() -> list:
    """(router, prefix) for every router the installed captains declare, the core's
    first, each captain's in its manifest's order (compass_core.captains.routers)."""
    from compass_core import captains

    return captains.routers()


def _configure_environment() -> None:
    """Defaults that make the shared routers behave as a single-workspace install."""
    from compass_core import hosts
    from compass_core.config import workspace_config
    from compass_core.tables import tables
    from compass_core.workspace import default_workspace_id

    if not hosts.installed():
        # the open edition alone: no closed captains, so their legacy flags stay off
        os.environ.setdefault("DISABLED_FEATURES", OPEN_DISABLED_FEATURES)
    workspace_id = default_workspace_id() or "local"
    os.environ.setdefault("DEFAULT_WORKSPACE", workspace_id)
    try:
        config = workspace_config(workspace_id)
    except LookupError:
        logger.warning("no workspace config yet for %s; run `compass init_config` first", workspace_id)
        return
    dash = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    if not os.getenv("APP_PASSWORD") and dash.get("password"):
        os.environ["APP_USERNAME"] = str(dash.get("username") or "admin")
        os.environ["APP_PASSWORD"] = str(dash["password"])
    if os.getenv("APP_PASSWORD"):
        logger.info("dashboard sign-in: user %s (`compass login` shows the password)", os.getenv("APP_USERNAME") or "admin")
    else:
        logger.warning("NO DASHBOARD PASSWORD: sign-in is off and anyone who can reach the port can use Compass. Set APP_PASSWORD in .env.")
    items = tables(config).items
    if items:
        # API tokens are two items per token; the items table holds them in the open edition.
        os.environ.setdefault("WORKSPACES_TABLE", items)


def _configure_queue() -> None:
    """Jobs started from the dashboard go to the jobs table; `compass serve` runs them."""
    from compass_core import anthropic_model, llm
    from compass_core import queue as _queue
    from compass_core.config import workspace_config
    from compass_core.dispatch import use_inline_queue
    from compass_core.jobs import store_for
    from compass_core.queue import StoreQueue, set_job_queue
    from compass_core.workspace import default_workspace_id

    if _queue._factory is not None:
        # a host installed its own queue (compass_core.hosts): keep it
        if llm.model_for() is None:
            anthropic_model.install_if_available()
        return
    store = None
    try:
        store = store_for(workspace_config(default_workspace_id() or "local"))
    except LookupError:
        pass
    if store is not None:
        queue = StoreQueue(store)
        set_job_queue(lambda: queue)
    else:
        logger.warning("no jobs table in the workspace config; jobs started from the API run inside the request")
        use_inline_queue()
    if llm.model_for() is None:
        anthropic_model.install_if_available()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run the hosts' startup hooks, then their shutdown hooks (compass_api.extensions)."""
    import inspect

    from compass_api import extensions as _extensions

    for hook in _extensions.startup_hooks():
        result = hook()
        if inspect.isawaitable(result):
            await result
    yield
    for hook in _extensions.shutdown_hooks():
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - one hook failing does not skip the others
            logger.exception("shutdown hook %s failed", getattr(hook, "__name__", hook))


def create_app() -> FastAPI:
    from compass_core import hosts

    hosts.install()  # a host (the product on top of Compass) sets its seams before the open defaults
    _configure_environment()
    _configure_queue()

    from compass_api.captain_gate import CaptainGateMiddleware, refusal
    from compass_api.middleware.bearer_auth import BearerAuthMiddleware
    from compass_api.middleware.simple_auth import SimpleAuthMiddleware
    from compass_api.middleware.workspace import WorkspaceContextMiddleware
    from compass_api.routes import settings
    from compass_core import captain_admin, captains

    app = FastAPI(title="Compass", lifespan=_lifespan)
    # innermost: runs after the workspace middleware, so the gate sees the workspace config
    app.add_middleware(CaptainGateMiddleware)
    app.add_middleware(SimpleAuthMiddleware)
    app.add_middleware(WorkspaceContextMiddleware, header_name="X-Workspace-Id", optional=False)
    # Added after the workspace middleware so it runs first: an API token decides the workspace.
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _invalidate_bootstrap_cache(request, call_next):
        response = await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            workspace_ctx = getattr(request.state, "workspace_ctx", None)
            settings.invalidate_bootstrap_sections(getattr(workspace_ctx, "workspace_id", None))
        return response

    for router, prefix in routers():
        app.include_router(router, prefix=prefix)
    from compass_api import extensions as _extensions

    for router, prefix in _extensions.routers():
        app.include_router(router, prefix=prefix)

    @app.exception_handler(captains.CaptainUnavailable)
    async def _captain_unavailable(request, exc):  # noqa: ANN001
        return refusal(exc)

    @app.exception_handler(captain_admin.CaptainAdminError)
    async def _captain_admin_error(request, exc):  # noqa: ANN001
        return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.to_dict()})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app

