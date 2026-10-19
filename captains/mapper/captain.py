import logging

import boto3

from compass_core.aws import build_target_session, resolve_target_region
from compass_core.events import emit_job_event
from compass_core.tables import tables
from infra_map.graph_store import open_store

from .aws_mapper import AWSInfraMapper
from .context import ExecutionContext

logger = logging.getLogger("mapper-captain")
logging.basicConfig(level=logging.INFO)


def _workspace_id_from_config(config) -> str:
    candidate = (
        (config or {}).get("workspace_id")
        or (config or {}).get("target")
        or (config or {}).get("name")
        or (config or {}).get("org_id")
    )
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    raise RuntimeError("workspace_id is required for mapper runs")


class MapperCaptain:
    def __init__(self, config):
        self.config = config or {}
        self.ctx = None
        self.infra_mapper = None
        self.enable_stage1 = bool(self.config.get("enable_stage1", False))

    def run(self):
        logger.info("🧠 Starting Mapper Captain...")
        try:
            emit_job_event("Initializing infra scan", event_type="progress", status="running")
            self._load_target_session()
            emit_job_event("Preparing mapper components", event_type="progress", status="running")
            self._setup_components()
        except Exception as exc:
            logger.error("❌ Mapper initialization failed: %s", exc)
            emit_job_event(f"Infra scan initialization failed: {exc}", event_type="progress", status="failed")
            raise

        if self.enable_stage1:
            logger.info("⚙️ Legacy stage 1 requested, but infra_map is now the primary pipeline.")

        if not self.infra_mapper:
            emit_job_event("Infra mapper was not initialized", event_type="progress", status="failed")
            raise RuntimeError("Infra mapper was not initialized")

        logger.info("🌐 Building infra_map...")
        emit_job_event("Building infra map", event_type="progress", status="running")
        try:
            self.infra_mapper.run()
        except Exception as exc:
            emit_job_event(f"Infra scan failed: {exc}", event_type="progress", status="failed")
            raise
        emit_job_event("Infra scan completed", event_type="progress", status="completed")

    def _load_target_session(self):
        control_session = self._get_local_control_session()
        region = resolve_target_region(self.config, default=self.config.get("aws_region"))
        target_session = build_target_session(self.config, capability="read", region=region)

        if target_session is None:
            raise RuntimeError("target AWS credentials are required for mapper scans")

        self.ctx = ExecutionContext(
            org_id=self.config["org_id"],
            request_id=self.config["request_id"],
            target_session=target_session,
            control_session=control_session,
        )

    def _get_local_control_session(self):
        return boto3.Session()

    def _setup_components(self):
        if not self.ctx:
            raise RuntimeError("execution context is not initialized")

        mapper_regions = self.config.get("aws_regions")
        if not mapper_regions:
            fallback_region = self.config.get("aws_region")
            mapper_regions = [fallback_region] if fallback_region else None

        workspace_id = _workspace_id_from_config(self.config)
        self.infra_mapper = AWSInfraMapper(
            org_id=self.ctx.org_id,
            workspace_id=workspace_id,
            target_session=self.ctx.target_session,
            control_session=self.ctx.control_session,
            table_name=tables(self.config).map,
            store=open_store(self.config, workspace_id=workspace_id, session=self.ctx.control_session),
            regions=mapper_regions,
            github_config=self.config.get("github"),
        )
