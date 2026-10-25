"""Verification (docs/specs/patching-captain.md §3.6): three steps recorded on each finding of a
pull request and told to the pull request, each able to keep it from being called ready.

1. **CI**: the repository's checks on the pull request's head, as the PR watcher reads them.
2. **The image build**: where the fix touched a Dockerfile, the check of the workflow that
   builds it: the repository's own, or the minimal ``compass-build.yml`` the pull request added.
3. **The canary**: when the workspace named a staging twin for the service (``patch.canaries``)
   and the grant ``patch.canary_deploy`` is armed, the pull request's image runs on the twin
   for a few minutes and is probed; the result is a comment on the pull request. A failed
   canary marks the pull request ``needs_human`` with ``canary_failed``. Without a twin or a
   grant the canary is skipped and the pull request says so; it is still ready on CI and
   the build.

The tick calls ``verify_all`` for every finding in ``pr_open``; the canary runs once per head.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from captains.patch import notify as _notify
from captains.patch.actions import CanaryContext, CanaryDeploy, CanaryScope, ScopeError
from captains.patch.findings import FindingStore

logger = logging.getLogger("patch.verify")

BUILD_WORKFLOW = "compass-build"
BUILD_WORDS = ("build", "docker", "image", "container")


def is_dockerfile(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile")


# ── CI and the build, from the watcher's record ─────────────────────────────

def ci_status(record: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    github = (record or {}).get("github") if isinstance((record or {}).get("github"), dict) else {}
    checks = github.get("checks") if isinstance(github.get("checks"), dict) else {}
    items = [i for i in checks.get("items") or [] if isinstance(i, dict)]
    total = int(checks.get("total") or 0)
    if not record or total == 0:
        return {"status": "pending" if record else "unknown", "detail": "no checks reported yet" if record else "the pull request is not tracked"}
    failed = [str(i.get("name")) for i in items if i.get("status") == "failure"]
    if int(checks.get("failed") or 0) > 0:
        return {"status": "failed", "detail": "failing: " + ", ".join(failed) if failed else f"{checks.get('failed')} check(s) failing", "checks": failed}
    if int(checks.get("pending") or 0) > 0:
        return {"status": "pending", "detail": f"{checks.get('pending')} of {total} check(s) still running"}
    return {"status": "passed", "detail": f"{total} check(s) green", "head_sha": github.get("head_sha")}


def build_status(record: Optional[Mapping[str, Any]], files: List[str]) -> Dict[str, Any]:
    if not any(is_dockerfile(f) for f in files):
        return {"status": "n/a", "detail": "the fix touches no Dockerfile"}
    github = (record or {}).get("github") if isinstance((record or {}).get("github"), dict) else {}
    checks = github.get("checks") if isinstance(github.get("checks"), dict) else {}
    items = [i for i in checks.get("items") or [] if isinstance(i, dict) and i.get("name")]
    builds = [i for i in items if BUILD_WORKFLOW in str(i["name"]).lower() or any(w in str(i["name"]).lower() for w in BUILD_WORDS)]
    if not items:
        return {"status": "pending", "detail": "no checks reported yet"}
    if not builds:
        return {"status": "missing", "detail": "no check builds the image; the pull request adds compass-build.yml when the repository has no build workflow"}
    if any(i.get("status") == "failure" for i in builds):
        return {"status": "failed", "detail": "the image build failed: " + ", ".join(str(i["name"]) for i in builds if i.get("status") == "failure")}
    if any(i.get("status") == "pending" for i in builds):
        return {"status": "pending", "detail": "the image build is running"}
    return {"status": "passed", "detail": "built: " + ", ".join(str(i["name"]) for i in builds)}


def build_workflow(dockerfiles: List[str]) -> str:
    """The minimal workflow the pull request adds when the repository has none that builds the image:
    each changed Dockerfile built with BuildKit on pull requests, not pushed."""
    paths = sorted(set(dockerfiles))
    matrix = "\n".join(f'          - {{ file: "{p}", context: "{p.rsplit("/", 1)[0] if "/" in p else "."}" }}' for p in paths)
    return f"""# Added by Compass's patch captain: the image must build before the fix is called ready.
# Keep it, or replace it with the workflow that builds and pushes your image on pull requests.
name: {BUILD_WORKFLOW}
on:
  pull_request:
    paths:
{chr(10).join(f'      - "{p}"' for p in paths)}
jobs:
  build:
    name: build ${{{{ matrix.file }}}}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
{matrix}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/build-push-action@v6
        with:
          context: ${{{{ matrix.context }}}}
          file: ${{{{ matrix.file }}}}
          push: false
          cache-from: type=gha
          cache-to: type=gha,mode=max
"""


def repo_builds_on_pull_requests(workflows: List[Mapping[str, Any]]) -> bool:
    """Whether one of the repository's workflows (map nodes) builds an image on pull requests."""
    for workflow in workflows:
        events = [str(e).lower() for e in workflow.get("events") or []]
        name = str(workflow.get("name") or "").lower()
        jobs = [str(j).lower() for j in workflow.get("jobs") or []]
        if any(e.startswith("pull_request") for e in events) and (workflow.get("publishes") or any(w in name for w in BUILD_WORDS) or any(any(w in j for w in BUILD_WORDS) for j in jobs)):
            return True
    return False


# ── the canary ───────────────────────────────────────────────────────────────

def canary_for(finding_rows: List[Mapping[str, Any]], config: Mapping[str, Any], services: Mapping[str, Any]) -> Dict[str, Any]:
    """Which staging twin the pull request's findings have, if any: ``patch.canaries`` is keyed by the
    production service's name; ``services`` maps the findings' task definitions to their services."""
    section = config.get("patch") if isinstance(config.get("patch"), dict) else {}
    canaries = section.get("canaries") if isinstance(section.get("canaries"), dict) else {}
    if not canaries:
        return {"status": "skipped", "detail": "no staging twin named in patch.canaries"}
    for row in finding_rows:
        for workload in row.get("workloads") or []:
            for service in services.get(str(workload.get("node_id")), []):
                name = str(service.get("name") or "")
                twin = canaries.get(name) or canaries.get(str(service.get("node_id")))
                if twin:
                    return {"status": "configured", "service": name, "service_arn": service.get("node_id"), "twin": twin, "finding_key": row.get("key")}
    return {"status": "skipped", "detail": "none of the pull request's workloads has a staging twin in patch.canaries"}


def _staging_service_arn(twin: Mapping[str, Any], production_arn: str, region: str, account_id: str) -> str:
    """``patch.canaries.<svc>.staging_service`` as an ARN: given as one, or built from the cluster and name."""
    staging = str(twin.get("staging_service") or "")
    if staging.startswith("arn:"):
        return staging
    cluster = str(twin.get("cluster") or "")
    if not cluster:
        parts = production_arn.split("/")
        cluster = parts[1] if len(parts) >= 3 else ""
    prefix = production_arn.rsplit(":", 1)[0] if production_arn.startswith("arn:") else f"arn:aws:ecs:{region}:{account_id}"
    return f"{prefix}:service/{cluster}/{staging}"


def pr_image(session: Any, finding_rows: List[Mapping[str, Any]], head_sha: str, region: str) -> Optional[Dict[str, Any]]:
    """The pull request's image in ECR: the production image's repository with a ``pr-<sha>`` tag
    (the first 7 or the full sha), pushed by the repository's build workflow."""
    from captains.mapper.software import ecr_parts

    for row in finding_rows:
        for workload in row.get("workloads") or []:
            via = str(workload.get("via") or "")
            if not via.startswith("image "):
                continue
            repository = via[len("image "):]
            try:
                ecr = session.client("ecr", region_name=region)
                for tag in (f"pr-{head_sha[:7]}", f"pr-{head_sha}", head_sha[:7], head_sha):
                    try:
                        details = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}]).get("imageDetails") or []
                    except Exception:  # noqa: BLE001 - ImageNotFound and friends
                        continue
                    if details:
                        uri = f"{details[0].get('registryId')}.dkr.ecr.{region}.amazonaws.com/{repository}:{tag}"
                        if ecr_parts(uri):
                            return {"image": uri, "digest": details[0].get("imageDigest"), "repository": repository, "tag": tag}
            except Exception as exc:  # noqa: BLE001
                logger.debug("pr image lookup in %s failed: %s", repository, exc)
    return None


def run_canary(*, config: Mapping[str, Any], workspace_id: str, finding_rows: List[Mapping[str, Any]], twin: Mapping[str, Any], production_arn: str,
               image: str, grant_row: Mapping[str, Any], session: Any, sleep: Any = time.sleep, clock: Any = time.time, probe: Any = None) -> Dict[str, Any]:
    """Snapshot → execute → verify → rollback on the staging twin, through the armed grant; the timeline is the result."""
    from compass_core import grants
    from compass_core.aws import resolve_target_account_id, resolve_target_region

    region = str(twin.get("region") or resolve_target_region(dict(config)) or production_arn.split(":")[3] if production_arn.startswith("arn:") else "us-east-1")
    account_id = resolve_target_account_id(dict(config)) or (production_arn.split(":")[4] if production_arn.startswith("arn:") else "")
    staging_arn = _staging_service_arn(twin, production_arn, region, account_id)
    ctx = CanaryContext(region=region, account_id=account_id, service_arn=staging_arn, image=image, container=twin.get("container"),
                        probe_paths=[str(p) for p in twin.get("probe_paths") or ["/"]], probe_urls=[str(u) for u in twin.get("probe_urls") or []][:3],
                        minutes=int(twin.get("minutes") or 5), finding_keys=[str(r.get("key")) for r in finding_rows], probe=probe)
    action = CanaryDeploy()
    timeline: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {"status": "running", "staging_service": staging_arn, "image": image, "started_at": _iso(clock()), "timeline": timeline}
    if not grants.is_armed(dict(grant_row)):
        result.update(status="skipped", detail=f"the grant patch.canary_deploy is {grant_row.get('grant_status') or 'not granted'}, not armed")
        return result
    try:
        action.check_scope(CanaryScope.from_dict(grant_row.get("scope")), ctx)
    except ScopeError as exc:
        result.update(status="skipped", detail=str(exc))
        return result
    snapshot: Optional[Dict[str, Any]] = None
    try:
        snapshot = action.snapshot(session, ctx)
        timeline.append({"step": "snapshot", "at": _iso(clock()), "task_definition": snapshot["task_definition"], "probes": snapshot.get("probe_urls")})
        executed = action.execute(session, ctx, snapshot)
        timeline.append({"step": "execute", "at": _iso(clock()), "task_definition": executed["task_definition"], "container": executed["container"]})
        verified = action.verify(session, ctx, snapshot, executed, sleep=sleep, clock=clock)
        timeline.append({"step": "verify", "at": _iso(clock()), **{k: v for k, v in verified.items() if k != "probes"}, "probes": (verified.get("probes") or [])[-5:]})
        result.update(status="passed" if verified.get("ok") else "failed", detail=verified.get("reason") or f"{verified.get('probe_count', 0)} probe(s) answered as before",
                      verify=verified)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the twin is rolled back below
        timeline.append({"step": "error", "at": _iso(clock()), "error": f"{type(exc).__name__}: {exc}"[:300]})
        result.update(status="failed", detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if snapshot is not None:
            try:
                rolled = action.rollback(session, ctx, snapshot)
                timeline.append({"step": "rollback", "at": _iso(clock()), **rolled})
            except Exception as exc:  # noqa: BLE001
                timeline.append({"step": "rollback", "at": _iso(clock()), "error": f"{type(exc).__name__}: {exc}"[:300]})
                result["rollback_failed"] = True
    result["finished_at"] = _iso(clock())
    return result


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── the whole step, per pull request ─────────────────────────────────────────

def verify_all(*, config: Mapping[str, Any], workspace_id: str, findings: FindingStore, registry: Any, services: Mapping[str, Any],
               grant_rows: List[Mapping[str, Any]], session_factory: Any = None, read_session: Any = None, comment: Any = None,
               set_watch: Any = None, now_ms: Optional[int] = None, sleep: Any = time.sleep, clock: Any = time.time, probe: Any = None) -> Dict[str, Any]:
    """For every open fix pull request: CI and the build from the watcher's record, the canary once per
    head when CI and the build are green; the verification written on each finding."""
    now = now_ms or int(time.time() * 1000)
    rows = findings.list(states=["pr_open"])
    by_pr: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        pr = row.get("pr") or {}
        if pr.get("repo") and pr.get("number"):
            by_pr.setdefault(f"{pr['repo']}#{pr['number']}", []).append(row)
    report: Dict[str, Any] = {"pull_requests": len(by_pr), "canaries": [], "needs_human": []}
    for key, group in by_pr.items():
        repo, _, number = key.rpartition("#")
        record = registry.get(repo, int(number)) if registry is not None else None
        pr = dict(group[0].get("pr") or {})
        previous = dict(pr.get("verification") or {})
        files = [str(f) for f in pr.get("files") or []]
        verification: Dict[str, Any] = {"ci": ci_status(record), "build": build_status(record, files), "canary": previous.get("canary") or canary_for(group, config, services),
                                        "checked_at": now}
        head_sha = str(((record or {}).get("github") or {}).get("head_sha") or "")
        canary = verification["canary"]
        if canary.get("status") == "configured" or (canary.get("status") in ("passed", "failed", "skipped") and canary.get("head_sha") and canary.get("head_sha") != head_sha and canary.get("twin")):
            green = verification["ci"]["status"] == "passed" and verification["build"]["status"] in ("passed", "n/a")
            if green and head_sha:
                twin = canary.get("twin") or {}
                grant = next((g for g in grant_rows if str(g.get("action_type")) == CanaryDeploy.action_type), None)
                if grant is None:
                    canary = {**canary, "status": "skipped", "detail": "no grant for patch.canary_deploy: propose it on the Access page", "head_sha": head_sha}
                else:
                    image = pr_image(read_session, group, head_sha, str(twin.get("region") or (canary.get("service_arn") or "arn:aws:ecs:us-east-1").split(":")[3])) if read_session is not None else None
                    if image is None:
                        canary = {**canary, "status": "skipped", "detail": f"no image tagged pr-{head_sha[:7]} in ECR: the build workflow does not push for pull requests", "head_sha": head_sha}
                    else:
                        session = session_factory(grant) if session_factory is not None else None
                        if session is None:
                            canary = {**canary, "status": "skipped", "detail": "no act session: run `compass connect` and arm the grant", "head_sha": head_sha}
                        else:
                            outcome = run_canary(config=config, workspace_id=workspace_id, finding_rows=group, twin=twin, production_arn=str(canary.get("service_arn") or ""),
                                                 image=image["image"], grant_row=grant, session=session, sleep=sleep, clock=clock, probe=probe)
                            canary = {**canary, **outcome, "head_sha": head_sha, "twin": twin}
                            report["canaries"].append({"pr": key, "status": canary["status"], "detail": canary.get("detail")})
                            if comment is not None:
                                try:
                                    comment(repo, int(number), canary_comment(canary))
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug("canary comment on %s not posted: %s", key, exc)
                            if canary["status"] == "failed":
                                report["needs_human"].append(key)
                                if set_watch is not None:
                                    try:
                                        set_watch(repo, int(number), "needs_human", "canary_failed")
                                    except Exception as exc:  # noqa: BLE001
                                        logger.debug("watch on %s not set: %s", key, exc)
                                _notify.emit(dict(config), workspace_id, "patch.needs_human", {"repo": repo, "number": int(number), "reason": "canary_failed",
                                                                                                "detail": canary.get("detail"), "url": pr.get("url")})
            verification["canary"] = canary
        ready = verification["ci"]["status"] == "passed" and verification["build"]["status"] in ("passed", "n/a") and verification["canary"].get("status") in ("passed", "skipped", "n/a")
        verification["ready"] = ready
        for row in group:
            findings.update(str(row["key"]), pr={**(row.get("pr") or {}), "verification": verification})
    return report


def canary_comment(canary: Mapping[str, Any]) -> str:
    status = str(canary.get("status"))
    head = f"**Canary {status}** on `{canary.get('staging_service', '').rsplit('/', 1)[-1]}` with `{str(canary.get('image', '')).rsplit('/', 1)[-1]}`"
    lines = [head, "", str(canary.get("detail") or "")]
    for step in canary.get("timeline") or []:
        lines.append(f"- {step.get('step')} at {step.get('at')}" + (f": {step.get('reason')}" if step.get("reason") else "") + (f" ({step.get('error')})" if step.get("error") else ""))
    verify = canary.get("verify") or {}
    if verify.get("probes"):
        lines.append("")
        lines.append("| Probe | Answered | Before |")
        lines.append("|---|---|---|")
        for p in verify["probes"][-5:]:
            lines.append(f"| {p.get('url')} | {p.get('status')} | {p.get('expected') if p.get('expected') is not None else '—'} |")
    lines += ["", "_The staging service was rolled back to its previous revision. Compass's patch captain._"]
    return "\n".join(lines)
