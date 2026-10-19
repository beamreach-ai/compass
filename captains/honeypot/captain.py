from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from captains.honeypot.classes import normalize_class
from captains.honeypot.honeytoken_material import default_cover_name
from compass_core.events import emit_job_event
from compass_core.tables import dynamo_table, tables

logger = logging.getLogger("honeypot-captain")

_TFROOT_GITHUB_RE = re.compile(r"tfroot://github/([^/]+/[^/]+)/(.+)")


def _repo_from_tf_root_id(tf_root_id: str) -> Optional[str]:
    m = _TFROOT_GITHUB_RE.match(str(tf_root_id or ""))
    return m.group(1) if m else None


def _path_from_tf_root_id(tf_root_id: str) -> Optional[str]:
    m = _TFROOT_GITHUB_RE.match(str(tf_root_id or ""))
    return m.group(2) if m else None


def _repo_from_github_repo_id(node_id: str) -> Optional[str]:
    prefix = "github://repo/"
    s = str(node_id or "")
    return s[len(prefix):] if s.startswith(prefix) else None


def _published_branch(writer: Any) -> Optional[str]:
    """The branch the writer's last PR landed on, when it reported one."""
    last = getattr(writer, "last_pr", None)
    return getattr(last, "branch", None) if last else None


def discover_map_repos(graph: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Discover every repo known to the infra-map graph.

    Returns [(repo, module_count), ...] where module_count is how many
    tf_root_module nodes the repo owns. Ordering: repos that own Terraform
    modules first (most-used first), then all remaining repos alphabetically.
    Repos come from `github_repo` nodes (all repos) plus any repo referenced by
    a `tf_root_module` node. This lets the honeypot feature offer the full repo
    list and pick a sensible default without manual config.
    """
    tf_counts: Dict[str, int] = {}
    all_repos: set = set()
    for node in graph.get("nodes", []):
        ntype = getattr(node, "type", None)
        if ntype == "tf_root_module":
            attrs = getattr(node, "attributes", None) or {}
            repo = attrs.get("repo") or _repo_from_tf_root_id(getattr(node, "resource_id", "") or "")
            if repo:
                tf_counts[repo] = tf_counts.get(repo, 0) + 1
                all_repos.add(repo)
        elif ntype == "github_repo":
            repo = getattr(node, "name", None) or _repo_from_github_repo_id(getattr(node, "resource_id", "") or "")
            if repo:
                all_repos.add(repo)

    ordered: List[Tuple[str, int]] = []
    for repo in sorted(tf_counts, key=lambda r: (-tf_counts[r], r)):
        ordered.append((repo, tf_counts[repo]))
    for repo in sorted(all_repos - set(tf_counts)):
        ordered.append((repo, 0))
    return ordered


class HoneypotCaptain:
    def __init__(self, config: Dict[str, Any], workspace_id: str):
        self.config = config or {}
        self.workspace_id = workspace_id

    # ── Plant (create honeytoken and open PR) ────────────────────────────

    def run_plant(self, *, honeytoken: Dict[str, Any]) -> Dict[str, Any]:
        from captains.honeypot.pr_writer import HoneypotPRWriter
        from captains.honeypot.terraform_generator import generate_terraform, module_slug, root_outputs

        # The generators bake the workspace id into EventBridge templates as a literal.
        honeytoken = {**honeytoken, "workspace_id": honeytoken.get("workspace_id") or self.workspace_id}
        honeytoken_id = honeytoken.get("honeytoken_id", "")
        honeytoken_name = honeytoken.get("name") or default_cover_name(honeytoken_id)
        resource_path = honeytoken.get("resource_path") or ""

        emit_job_event(f"Generating Terraform for honeypot: {honeytoken_name}", event_type="progress", status="running")

        try:
            hcl_files = generate_terraform(honeytoken)
        except ValueError as exc:
            emit_job_event(str(exc), event_type="error", status="failed")
            raise

        # Resolve which repo/path this honeypot belongs in by looking at the
        # infra map neighbors of the target resource — same logic as IAC fix_drift.
        org_id = self.config.get("org_id") or self.workspace_id
        repo, module_path = self._resolve_repo_from_neighbors(resource_path, org_id=org_id)

        if repo:
            emit_job_event(
                f"Resolved target repo: {repo} (path: {module_path or 'root'}). Creating PR…",
                event_type="progress",
                status="running",
            )
        else:
            repo = self._resolve_default_repo(org_id=org_id)
            # The Terraform root inside the repo (e.g. environments/demo) when the
            # map cannot tell; without it the files would land at the repo root.
            module_path = module_path or (self.config.get("honeypot") or {}).get("terraform_root") or None
            if repo:
                emit_job_event(
                    f"No owning repo in infra map neighbors — using default repo: {repo} (path: {module_path or 'root'}).",
                    event_type="progress",
                    status="running",
                )
            else:
                emit_job_event(
                    "No repo found in infra map neighbors and no default configured.",
                    event_type="progress",
                    status="running",
                )

        try:
            writer = HoneypotPRWriter(self.config)
            pr_url = writer.create_plant_pr(
                honeytoken=honeytoken,
                hcl_files=hcl_files,
                resolved_repo=repo,
                resolved_module_path=module_path,
                module_slug=module_slug(honeytoken),
                queue_arn=self._alert_queue_arn(honeytoken),
                workspace_id=str(honeytoken.get("workspace_id") or self.workspace_id),
                root_outputs=root_outputs(honeytoken),
                extra_inputs=self._module_inputs(honeytoken),
                task_env=self._task_env(honeytoken),
            )
        except Exception as exc:
            emit_job_event(f"Failed to create PR: {exc}", event_type="error", status="failed")
            raise

        self._update_honeytoken_pr_url(honeytoken_id, pr_url)
        self._register_pr("plant", pr_url=pr_url, honeytoken=honeytoken, module_path=module_path, title=f"Honeypot: plant {honeytoken_name}", branch=_published_branch(writer))
        edited = getattr(writer, "task_env_result", None)
        if edited:
            self._update_placement(honeytoken_id, {**edited, "pr_url": pr_url, "repo": repo or ""})
            emit_job_event(
                f"Task definition {edited.get('tf_address')}: added {', '.join(edited.get('env_vars') or [])}",
                event_type="progress",
                status="running",
            )
        emit_job_event(f"Honeypot PR created: {pr_url}", event_type="progress", status="completed", data={"pr_url": pr_url})
        return {"pr_url": pr_url, "honeytoken_id": honeytoken_id}

    @staticmethod
    def _task_env(honeytoken: Dict[str, Any], *, remove: bool = False) -> Optional[Dict[str, Any]]:
        """For an `ecs_task_env` placement: which container gets which variables (HP-05).

        Plant wires each variable to the token module's output; decommission
        removes the names recorded on the placement (or the class defaults)."""
        from captains.honeypot.task_env import env_entries
        from captains.honeypot.terraform_generator import TOKEN_MODULE_PREFIX, module_slug

        placement = honeytoken.get("placement") if isinstance(honeytoken.get("placement"), dict) else {}
        if placement.get("type") != "ecs_task_env":
            return None
        entries = env_entries(str(honeytoken.get("honeytoken_class") or ""), f"{TOKEN_MODULE_PREFIX}{module_slug(honeytoken)}")
        if not entries:
            return None
        container = str(placement.get("container") or "").strip()
        if not container:
            raise RuntimeError("An ecs_task_env placement needs the container name (placement.container)")
        out: Dict[str, Any] = {"container": container, "tf_address": placement.get("tf_address") or None}
        if remove:
            out["names"] = list(placement.get("env_vars") or [name for name, _ in entries])
        else:
            out["entries"] = entries
        return out

    def _module_inputs(self, honeytoken: Dict[str, Any]) -> Dict[str, Any]:
        """Module inputs beyond the shared four. A decoy database service needs
        the network to run in and its image; both come from workspace config
        (`honeypot.network`, `honeypot.listener_image`) or the placement."""
        if normalize_class(honeytoken.get("honeytoken_class") or "") != "db_credential":
            return {}
        cfg = self.config.get("honeypot") if isinstance(self.config.get("honeypot"), dict) else {}
        network = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
        placement = honeytoken.get("placement") if isinstance(honeytoken.get("placement"), dict) else {}
        material = honeytoken.get("material") if isinstance(honeytoken.get("material"), dict) else {}
        inputs = {
            "vpc_id": network.get("vpc_id"),
            "subnet_ids": network.get("subnet_ids") or [],
            "cluster_arn": placement.get("cluster_arn") or network.get("cluster_arn"),
            "image": cfg.get("listener_image"),
            "db_user": material.get("user"),
            "db_password": material.get("password"),
        }
        missing = [k for k in ("vpc_id", "subnet_ids", "image") if not inputs.get(k)]
        if missing:
            raise RuntimeError(f"db_credential needs honeypot.network/listener_image in workspace config (missing: {', '.join(missing)})")
        return inputs

    def _alert_queue_arn(self, honeytoken: Dict[str, Any]) -> str:
        """ARN of the Compass alert queue the plant PR points rules at.

        Looked up on the token, then workspace config, then the environment."""
        cfg = self.config.get("honeypot") or {}
        arn = (
            honeytoken.get("alert_queue_arn")
            or cfg.get("alert_queue_arn")
            or self.config.get("beamreach_honeypot_alert_queue_arn")
            or os.getenv("HONEYPOT_ALERT_QUEUE_ARN")
            or ""
        )
        if not arn:
            raise RuntimeError(
                "No alert queue ARN configured (honeypot.alert_queue_arn or HONEYPOT_ALERT_QUEUE_ARN); "
                "a plant PR cannot deliver alerts without it."
            )
        return str(arn)

    # ── Decommission (remove honeytoken and open removal PR) ────────────

    def run_decommission(self, *, honeytoken: Dict[str, Any]) -> Dict[str, Any]:
        from captains.honeypot.pr_writer import HoneypotPRWriter
        from captains.honeypot.terraform_generator import module_slug

        honeytoken_name = honeytoken.get("name") or default_cover_name(str(honeytoken.get("honeytoken_id", "")))
        resource_path = honeytoken.get("resource_path") or ""
        org_id = self.config.get("org_id") or self.workspace_id

        emit_job_event(f"Decommissioning honeypot: {honeytoken_name}", event_type="progress", status="running")

        repo, module_path = self._resolve_repo_from_neighbors(resource_path, org_id=org_id)
        if not repo:
            repo = self._resolve_default_repo(org_id=org_id)
            # Same root the plant PR used (run_plant falls back to it too).
            module_path = module_path or (self.config.get("honeypot") or {}).get("terraform_root") or None

        try:
            writer = HoneypotPRWriter(self.config)
            pr_url = writer.create_decommission_pr(
                honeytoken=honeytoken,
                module_slug=module_slug(honeytoken),
                resolved_repo=repo,
                resolved_module_path=module_path,
                task_env=self._task_env(honeytoken, remove=True),
            )
        except Exception as exc:
            emit_job_event(f"Failed to create decommission PR: {exc}", event_type="error", status="failed")
            raise

        self._register_pr("decommission", pr_url=pr_url, honeytoken=honeytoken, module_path=module_path, title=f"Honeypot: decommission {honeytoken_name}", branch=_published_branch(writer))
        emit_job_event(f"Decommission PR created: {pr_url}", event_type="progress", status="completed", data={"pr_url": pr_url})
        return {"pr_url": pr_url, "honeytoken_id": honeytoken.get("honeytoken_id", "")}

    # ── Alert notification dispatch ────────────────────────────────────────

    def run_alert_notify(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        from captains.honeypot.alert_dispatcher import dispatch_alert

        emit_job_event("Dispatching alert notifications", event_type="progress", status="running")

        dynamodb_config = self.config.get("dynamodb_config") or {}
        try:
            dispatch_alert(
                workspace_id=self.workspace_id,
                dynamodb_config=dynamodb_config,
                honeytoken_id=str(payload.get("honeytoken_id", "")),
                honeytoken_name=str(payload.get("honeytoken_name", "")),
                honeytoken_class=str(payload.get("honeytoken_class", "")),
                alert_id=str(payload.get("alert_id", "")),
                source_ip=payload.get("source_ip"),
                ip_type=payload.get("ip_type"),
                principal_arn=payload.get("principal_arn"),
                triggered_at=str(payload.get("triggered_at", "")),
                action_config=payload.get("action_config") or {},
                placement_note=payload.get("placement_note"),
                is_test=bool(payload.get("is_test", False)),
                relay_license_key=(self.config.get("billing") or {}).get("license_key"),
                relay_install_id=(self.config.get("billing") or {}).get("install_id"),
                relay_api_url=os.getenv("LICENSING_API_URL"),
                incident_id=payload.get("incident_id"),
                repeat_count=payload.get("repeat_count"),
                related_incident_id=payload.get("related_incident_id"),
                event_time=payload.get("event_time"),
                received_at=payload.get("received_at"),
                workload=payload.get("workload"),
                workspace_config=self.config,
            )
        except Exception as exc:
            emit_job_event(f"Alert dispatch failed: {exc}", event_type="error", status="failed")
            raise

        emit_job_event("Alert notifications dispatched", event_type="progress", status="completed")
        return {"status": "dispatched"}

    # ── Manual runbook dispatch ────────────────────────────────────────────

    # ── Investigation (HP-08) ─────────────────────────────────────────────

    def run_investigate(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Pin the source task, its role and what that role can reach, and
        record it on the incident. Runs after the first alert has gone out."""

        from captains.honeypot.investigate import investigate_incident, load_map_store_for_config
        from compass_core.aws import build_target_session, resolve_target_region
        from compass_core.keys import workspace_pk

        incident_id = str(payload.get("incident_id") or "")
        if not incident_id:
            raise ValueError("investigate needs incident_id")
        emit_job_event(f"Investigating incident {incident_id}", event_type="progress", status="running")

        table = dynamo_table(tables(self.config).items)
        pk = workspace_pk(self.workspace_id)

        target_region = str(payload.get("region") or resolve_target_region(self.config) or "us-east-1")
        try:
            session = build_target_session(self.config, capability="read", region=target_region)
        except Exception as exc:  # noqa: BLE001 - investigate what we can without AWS
            emit_job_event(f"No target AWS access for correlation: {exc}", event_type="warning", status="running")
            session = None
        store, org_id = load_map_store_for_config(self.config, self.workspace_id)

        result = investigate_incident(
            table=table, pk=pk, incident_id=incident_id, session=session,
            region=target_region, store=store, org_id=org_id,
        )
        blast = result.get("blast_radius") or {}
        task = (result.get("correlation") or {}).get("task") or {}
        emit_job_event(
            f"Investigation done: task {task.get('task_id') or 'unresolved'}, blast radius {blast.get('total', 0)} ({blast.get('method')})",
            event_type="progress", status="completed",
        )
        self._post_update(
            incident_id,
            f"Investigated: source task {task.get('task_id') or 'unresolved'}, "
            f"blast radius {blast.get('total', 0)} ({blast.get('method')}).",
        )
        # The response is its own job so a slow or failed containment never
        # hides the investigation result.
        try:
            from compass_core.queue import job_queue

            job_queue().enqueue(
                captain="honeypot",
                command="respond",
                payload={"incident_id": incident_id, "region": target_region, "workspace_id": self.workspace_id},
            )
        except Exception as exc:  # noqa: BLE001
            emit_job_event(f"Response could not be queued: {exc}", event_type="warning", status="completed")
        return result

    # ── Response (HP-10 / HP-11) ──────────────────────────────────────────

    def _post_update(self, incident_id: str, text: str, kind: str = "update") -> None:
        """Thread/email update for an incident; never raises."""
        from captains.honeypot.alert_dispatcher import post_incident_update

        try:
            post_incident_update(
                self.config.get("dynamodb_config") or {},
                self.workspace_id,
                incident_id,
                text,
                subject=f"Honeypot incident {incident_id}: {kind}",
                relay_license_key=(self.config.get("billing") or {}).get("license_key"),
                relay_install_id=(self.config.get("billing") or {}).get("install_id"),
                relay_api_url=os.getenv("LICENSING_API_URL"),
                workspace_config=self.config,
            )
        except Exception as exc:  # noqa: BLE001
            emit_job_event(f"Incident update not delivered: {exc}", event_type="warning", status="running")

    def run_respond(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Choose among the whitelisted actions, gate, run, record, report."""

        from captains.honeypot import incidents
        from captains.honeypot.respond import agent_invoke_from_env, respond_to_incident, responder_session_factory
        from captains.honeypot.summary import build_incident_facts, summarize_incident
        from compass_core.aws import resolve_target_account_id, resolve_target_region
        from compass_core.keys import workspace_pk

        incident_id = str(payload.get("incident_id") or "")
        if not incident_id:
            raise ValueError("respond needs incident_id")
        emit_job_event(f"Responding to incident {incident_id}", event_type="progress", status="running")

        table = dynamo_table(tables(self.config).items)
        pk = workspace_pk(self.workspace_id)
        region = str(payload.get("region") or resolve_target_region(self.config) or "us-east-1")
        account_id = resolve_target_account_id(self.config, "respond") or resolve_target_account_id(self.config, "read") or ""

        incident = incidents.get_incident(table, pk, incident_id) or {}
        alerts = incidents.alerts_for_incident(table, pk, incident_id)
        audits = incidents.audits_for_incident(table, pk, incident_id)
        facts = build_incident_facts(incident, alerts, audits)
        honeypot_cfg = self.config.get("honeypot") if isinstance(self.config.get("honeypot"), dict) else {}

        result = respond_to_incident(
            table=table, pk=pk, incident_id=incident_id, region=region, account_id=account_id,
            session_factory=responder_session_factory(self.config, region),
            facts=facts, invoke=agent_invoke_from_env(),
            notify=lambda text, kind: self._post_update(incident_id, text, kind),
            calibration_cfg=honeypot_cfg.get("risk_calibration") if isinstance(honeypot_cfg.get("risk_calibration"), dict) else None,
        )
        decisions = (result.get("action") or {}).get("decisions") or []
        emit_job_event(
            "Response: " + (", ".join(f"{d['action_type']}={d.get('status') or d.get('decision')}" for d in decisions) or "nothing to do"),
            event_type="progress", status="running",
        )

        # The report: summary from the facts as they now stand, posted to the thread.
        try:
            incident = incidents.get_incident(table, pk, incident_id) or incident
            audits = incidents.audits_for_incident(table, pk, incident_id)
            summary = summarize_incident(build_incident_facts(incident, alerts, audits))
            incidents.mark(table, pk, incident_id, summary=summary)
            bullets = "\n".join(f"• {b}" for b in summary.get("bullets", []))
            self._post_update(incident_id, f"{summary.get('paragraph', '')}\n{bullets}".strip(), "report")
            incidents.mark(table, pk, incident_id, report_sent_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:  # noqa: BLE001 - the report is best effort
            emit_job_event(f"Report not generated: {exc}", event_type="warning", status="running")

        emit_job_event("Response complete", event_type="progress", status="completed")
        return result

    # ── Responder role PR (HP-27) ──────────────────────────────────────────

    def run_responder_pr(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Turn the enabled whitelist rows into the responder IAM role, delivered
        as a PR against the honeypot repository like plants are."""

        from captains.honeypot.pr_writer import HoneypotPRWriter
        from captains.honeypot.respond import load_action_rows
        from captains.honeypot.responder_role import MODULE_DIR, MODULE_NAME, generate_responder_module
        from compass_core.aws import resolve_target_account_id, resolve_target_region
        from compass_core.keys import workspace_pk

        emit_job_event("Generating the responder role", event_type="progress", status="running")
        table = dynamo_table(tables(self.config).items)
        rows = load_action_rows(table, workspace_pk(self.workspace_id))
        hp_cfg = self.config.get("honeypot") if isinstance(self.config.get("honeypot"), dict) else {}
        responder = hp_cfg.get("responder") if isinstance(hp_cfg.get("responder"), dict) else {}
        trust = list(responder.get("trust_principal_arns") or [])
        if not trust:
            raise RuntimeError("honeypot.responder.trust_principal_arns is not configured")
        account_id = responder.get("account_id") or resolve_target_account_id(self.config, "respond") or resolve_target_account_id(self.config, "read") or ""
        region = responder.get("region") or resolve_target_region(self.config) or "us-east-1"
        module = generate_responder_module(
            rows, account_id=account_id, region=region, trust_principal_arns=trust,
            external_id=responder.get("external_id") or None, role_name=responder.get("role_name") or "svc-ops-responder",
        )
        if not module["enabled_action_types"]:
            raise RuntimeError("no enabled response actions; nothing to grant")

        writer = HoneypotPRWriter(self.config)
        repo = payload.get("repo") or self._resolve_default_repo(org_id=str(self.config.get("org_id") or ""))
        body = (
            "Operations responder role generated from the enabled response actions "
            f"({', '.join(module['enabled_action_types'])}). The role is assumed by the "
            "operations tooling with an external id and may do nothing beyond the listed actions.\n\n"
            "Review the policy in `services/_responder/main.tf`."
        )
        pr_url = writer.create_module_pr(
            module_name=MODULE_NAME, module_dir=MODULE_DIR, files=module["files"],
            outputs={"responder_role_arn": "role_arn"}, branch_suffix="responder-role",
            title="Operations responder role", body=body, resolved_repo=repo,
            resolved_module_path=hp_cfg.get("terraform_root") or None,
        )
        self._register_pr("responder_pr", pr_url=pr_url, honeytoken={"name": "responder role"}, module_path=hp_cfg.get("terraform_root") or None, title="Operations responder role", module_name=MODULE_NAME, module_dir=MODULE_DIR, branch_suffix="responder-role", branch=_published_branch(writer))
        emit_job_event(f"Responder role PR: {pr_url}", event_type="progress", status="completed")
        return {"pr_url": pr_url, "role_arn": module["role_arn"], "enabled_action_types": module["enabled_action_types"]}

    def run_dispatch_runbook(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        from captains.honeypot.alert_dispatcher import dispatch_alert

        emit_job_event("Dispatching runbook notifications", event_type="progress", status="running")

        dynamodb_config = self.config.get("dynamodb_config") or {}
        action_config = {
            "notification_endpoint_ids": payload.get("notification_endpoint_ids") or [],
            "severity": "high",
            "runbook": payload.get("runbook", ""),
        }
        try:
            dispatch_alert(
                workspace_id=self.workspace_id,
                dynamodb_config=dynamodb_config,
                honeytoken_id=str(payload.get("honeytoken_id", "")),
                honeytoken_name=str(payload.get("honeytoken_name", payload.get("honeytoken_id", ""))),
                honeytoken_class="manual_dispatch",
                alert_id="manual",
                source_ip=None,
                ip_type=None,
                principal_arn=None,
                triggered_at=str(payload.get("triggered_at", "")),
                action_config=action_config,
                runbook=payload.get("runbook"),
                workspace_config=self.config,
                relay_license_key=(self.config.get("billing") or {}).get("license_key"),
                relay_install_id=(self.config.get("billing") or {}).get("install_id"),
                relay_api_url=os.getenv("LICENSING_API_URL"),
            )
        except Exception as exc:
            emit_job_event(f"Runbook dispatch failed: {exc}", event_type="error", status="failed")
            raise

        emit_job_event("Runbook dispatched", event_type="progress", status="completed")
        return {"status": "dispatched"}

    # ── Default repo (config, else most-used repo discovered in the map) ───

    def _resolve_default_repo(self, *, org_id: str) -> Optional[str]:
        """Fallback target repo when the resource has no owning repo in the map.

        Precedence: an explicitly chosen honeypot_repo, then a configured
        iac_repo, then the most-used Terraform repo discovered in the infra map.
        """
        github_cfg = self.config.get("github") or {}
        configured = github_cfg.get("honeypot_repo") or github_cfg.get("iac_repo")
        if configured:
            return str(configured)

        try:
            store = self._load_store()
            graph = store.fetch_graph(org_id)
        except Exception as exc:
            logger.warning("Cannot discover default repo from map: %s", exc)
            return None

        discovered = discover_map_repos(graph)
        # Prefer a repo that actually owns Terraform (module_count > 0); the
        # generated HCL has to land somewhere Terraform-managed.
        tf_repos = [repo for repo, count in discovered if count > 0]
        chosen = tf_repos[0] if tf_repos else (discovered[0][0] if discovered else None)
        if chosen:
            logger.info("Auto-selected default honeypot repo from map: %s", chosen)
        return chosen

    # ── Repo resolution from infra map neighbors ───────────────────────────

    def _resolve_repo_from_neighbors(
        self,
        resource_id: str,
        *,
        org_id: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Walk the infra map from `resource_id` toward the tf_root_module that
        manages it (same graph traversal as IACFixDriftJob._batch_lookup_ownership).
        Returns (repo, module_path) or (None, None) if nothing is found.
        """
        if not resource_id:
            return None, None

        try:
            store = self._load_store()
        except Exception as exc:
            logger.warning("Cannot load infra map store for repo resolution: %s", exc)
            return None, None

        try:
            graph = store.fetch_graph(org_id)
        except Exception as exc:
            logger.warning("Cannot fetch infra map graph: %s", exc)
            return None, None

        all_nodes = {n.resource_id: n for n in graph.get("nodes", [])}

        # Build reverse-edge index: target_id → list of edges pointing at it
        incoming: Dict[str, List] = defaultdict(list)
        for edge in graph.get("edges", []):
            incoming[edge.to_resource_id].append(edge)

        # Step 1: find the tf_state_instance that "observes" our resource
        obs_edges = [e for e in incoming.get(resource_id, []) if e.relationship_type == "observes"]
        if not obs_edges:
            logger.debug("No 'observes' edge found for resource %s — trying neighbors", resource_id)
            # Broaden: look at direct neighbors (any edge) and try ownership on each
            neighbor_ids = {e.from_resource_id for e in incoming.get(resource_id, [])}
            neighbor_ids |= {e.to_resource_id for edge_list in [
                [e for e in graph.get("edges", []) if e.from_resource_id == resource_id]
            ] for e in edge_list}
            for nid in neighbor_ids:
                repo, path = self._ownership_from_state_instance(nid, all_nodes, incoming)
                if repo:
                    logger.info("Resolved repo %s from neighbor %s of %s", repo, nid, resource_id)
                    return repo, path
            return None, None

        instance_id = obs_edges[0].from_resource_id
        return self._ownership_from_state_instance(instance_id, all_nodes, incoming)

    def _ownership_from_state_instance(
        self,
        instance_id: str,
        all_nodes: Dict[str, Any],
        incoming: Dict[str, List],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Given a tf_state_instance node ID, walk up to tf_root_module and return (repo, path)."""
        # Step 2: find the tf_state_file that "contains" this instance
        state_file = None
        for e in incoming.get(instance_id, []):
            if e.relationship_type == "contains":
                state_file = all_nodes.get(e.from_resource_id)
                break

        if not state_file:
            return None, None

        # Step 3: find the tf_root_module that "writes" to this state file
        tf_root = None
        for e in incoming.get(state_file.resource_id, []):
            if e.relationship_type == "writes":
                tf_root = all_nodes.get(e.from_resource_id)
                break

        if not tf_root:
            return None, None

        tf_root_attrs = tf_root.attributes or {}
        repo = tf_root_attrs.get("repo") or _repo_from_tf_root_id(tf_root.resource_id)
        module_path = tf_root_attrs.get("path") or _path_from_tf_root_id(tf_root.resource_id)

        logger.info("Resolved ownership: resource → tf_root %s → repo=%s path=%s", tf_root.resource_id, repo, module_path)
        return repo, module_path

    def _load_store(self):
        from infra_map.graph_store import open_store

        return open_store(self.config, workspace_id=self.config.get("workspace_id") or self.workspace_id)

    def _register_pr(
        self,
        command: str,
        *,
        pr_url: str,
        honeytoken: Dict[str, Any],
        module_path: Optional[str],
        title: str,
        module_name: Optional[str] = None,
        module_dir: Optional[str] = None,
        branch_suffix: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> None:
        """Register the PR with its intent so the watcher can keep it alive (docs/pr_watcher_spec.md §2).

        `branch` is the one the writer published to; it is only re-derived here
        when the writer did not report it."""
        try:
            from captains.honeypot.pr_writer import _BRANCH_PREFIX, _safe_branch_suffix
            from captains.honeypot.terraform_generator import ROOT_FILE, SERVICES_DIR, TOKEN_MODULE_PREFIX, module_slug
            from captains.prs.intent import intent_for_honeypot
            from compass_core.events import current_job_id
            from compass_core.registry import PRRegistry

            root = (module_path or "").strip("/")
            prefix = f"{root}/" if root else ""
            name = str(honeytoken.get("name") or default_cover_name(str(honeytoken.get("honeytoken_id", ""))))
            if command == "responder_pr":
                derived_branch = f"{_BRANCH_PREFIX}{_safe_branch_suffix(branch_suffix or 'responder-role')}"
                module = module_name or "svc_responder"
                directory = f"{prefix}{module_dir or 'services/_responder'}"
                files = [f"{prefix}{ROOT_FILE}"]
            else:
                slug = module_slug(honeytoken)
                module = f"{TOKEN_MODULE_PREFIX}{slug}"
                directory = f"{prefix}{SERVICES_DIR}/{slug}"
                files = [f"{prefix}{ROOT_FILE}"]
                suffix = _safe_branch_suffix(name)
                derived_branch = f"{_BRANCH_PREFIX}{'decommission-' if command == 'decommission' else ''}{suffix}"
            branch = branch or derived_branch
            github_cfg = self.config.get("github") or {}
            base_branch = str(github_cfg.get("default_branch") or github_cfg.get("base_branch") or "main")
            PRRegistry.from_config(self.config, workspace_id=self.workspace_id).record(
                url=pr_url,
                pr_type={"plant": "honeypot_plant", "decommission": "honeypot_decommission"}.get(command, "honeypot_responder"),
                origin={"captain": "honeypot", "command": command, "job_id": current_job_id(), "honeytoken_id": honeytoken.get("honeytoken_id")},
                intent=intent_for_honeypot(command, honeytoken=honeytoken, module_name=module, root_file=f"{prefix}{ROOT_FILE}", module_dir=directory, files=files),
                branch=branch,
                base_branch=base_branch,
                title=title,
            )
        except Exception as exc:
            logger.warning("PR registry write failed: %s", exc)

    # ── DynamoDB helper ────────────────────────────────────────────────────

    def _update_placement(self, honeytoken_id: str, fields: Dict[str, Any]) -> None:
        """Record on the token's placement what the plant PR edited (tf_address, env_vars, pr_url, repo)."""
        import boto3

        from compass_core.keys import honeytoken_sort_key, workspace_pk

        table_name = tables(self.config).items
        fields = {k: v for k, v in fields.items() if k in ("tf_address", "env_vars", "pr_url", "repo") and v}
        if not table_name or not fields:
            return
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        try:
            table.update_item(
                Key={"pk": workspace_pk(self.workspace_id), "sk": honeytoken_sort_key(honeytoken_id)},
                UpdateExpression="SET " + ", ".join(f"placement.#f{i} = :v{i}" for i in range(len(fields))),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(placement)",
            )
        except Exception as exc:
            logger.warning("Failed to record the placement edit: %s", exc)

    def _update_honeytoken_pr_url(self, honeytoken_id: str, pr_url: str) -> None:
        import time

        import boto3

        from compass_core.keys import honeytoken_sort_key, workspace_pk

        table_name = tables(self.config).items
        if not table_name:
            logger.warning("Cannot update pr_url — no DynamoDB table configured")
            return

        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        pk = workspace_pk(self.workspace_id)

        try:
            table.update_item(
                Key={"pk": pk, "sk": honeytoken_sort_key(honeytoken_id)},
                UpdateExpression="SET pr_url = :u, #st = :s, updated_at = :now",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":u": pr_url,
                    ":s": "pending_merge",
                    ":now": int(time.time() * 1000),
                },
            )
        except Exception as exc:
            logger.warning("Failed to update honeytoken pr_url: %s", exc)
