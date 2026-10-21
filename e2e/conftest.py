"""The end-to-end harness (docs/e2e.md): the stack, the fakes, a signed-in API, the
`compass` command and a browser, shared by every test in a run.

One stack per run. The session fixture builds the images from this checkout (skip
with E2E_BUILD=0 to reuse the last build), makes a scratch install from the shipped
docker-compose.yml plus e2e/compose.e2e.yml, seeds the fake AWS account and the fake
GitHub (e2e/fixture.py), and goes through the quickstart: init_config, init_tables,
up. Tests run in file order (test_00_… first) because later areas need what earlier
ones made: a map before decoys, decoys before incidents.

A failed test leaves, under e2e/.artifacts/<test>/, the browser's trace (open it with
`playwright show-trace trace.zip`), a screenshot and the stack's logs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest

from e2e import fixture

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "e2e"
ARTIFACTS = Path(os.getenv("E2E_ARTIFACTS") or E2E / ".artifacts")
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
IMAGES = {
    "COMPASS_IMAGE": "compass-e2e/compass:local",
    "COMPASS_DASHBOARD_IMAGE": "compass-e2e/dashboard:local",
    "COMPASS_UPDATER_IMAGE": "compass-e2e/updater:local",
}
PROJECT = "compass-e2e"
WEB_PORT = int(os.getenv("E2E_PORT") or 18480)
MOTO_PORT = int(os.getenv("E2E_MOTO_PORT") or 15000)
FAKES_PORT = int(os.getenv("E2E_FAKES_PORT") or 19000)


def _run(args: List[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, timeout: float = 900,
         check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}):\n{result.stderr[-3000:] or result.stdout[-3000:]}")
    return result


# ── the images ──────────────────────────────────────────────────────────────

def build_images() -> None:
    """The three images from this checkout, tagged compass-e2e/*:local."""
    dist = ROOT / "dist-e2e" / "suite"
    shutil.rmtree(dist, ignore_errors=True)
    _run(["python", "-m", "build", "--wheel", "--outdir", str(dist), str(ROOT)], timeout=600)
    wheel = next(dist.glob("*.whl")).relative_to(ROOT)
    _run(["docker", "build", "-q", "-f", "docker/Dockerfile", "--build-arg", f"WHEEL={wheel}", "-t", IMAGES["COMPASS_IMAGE"], "."], cwd=ROOT)
    _run(["docker", "build", "-q", "-f", "dashboard/Dockerfile.dashboard", "--build-arg", "NEXT_PUBLIC_EDITION=open",
          "--build-arg", f"COMPASS_VERSION={VERSION}", "-t", IMAGES["COMPASS_DASHBOARD_IMAGE"], "."], cwd=ROOT, timeout=1800)
    _run(["docker", "build", "-q", "-f", "docker/updater/Dockerfile", "-t", IMAGES["COMPASS_UPDATER_IMAGE"], "."], cwd=ROOT)
    shutil.rmtree(ROOT / "dist-e2e", ignore_errors=True)


def images_present() -> bool:
    return all(_run(["docker", "image", "inspect", image], check=False).returncode == 0 for image in IMAGES.values())


# ── the stack ───────────────────────────────────────────────────────────────

class Stack:
    """One install of Compass, made the way the quickstart says, with the fakes beside it."""

    def __init__(self, project: Path):
        self.project = project
        self.web = f"http://127.0.0.1:{WEB_PORT}"
        self.moto_url = f"http://127.0.0.1:{MOTO_PORT}"
        self.fakes_url = f"http://127.0.0.1:{FAKES_PORT}"
        self.records: Dict[str, Any] = {}   # what setup printed, for the install tests
        self.seeded: Dict[str, str] = {}
        self._api: Optional[httpx.Client] = None

    # the commands a person types
    def compose(self, *args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
        return _run(["docker", "compose", "-f", "docker-compose.yml", "-f", "compose.e2e.yml", "--profile", "updater", *args],
                    # PWD as a shell in the install would have it: the compose file mounts ${PWD} into the updater
                    cwd=self.project, env={"E2E_FAKES_DIR": str(E2E / "fakes"), "PWD": str(self.project)}, timeout=timeout, check=check)

    def cli(self, *args: str, expect: int = 0, timeout: float = 600) -> Any:
        """`docker compose run --rm compass <args>`: the JSON it prints, or (for an expected
        failure) its stderr. Asserts the exit code."""
        # --no-deps: the stack is up; never touch moto or the fakes, whose state lives in memory
        result = self.compose("run", "--rm", "-T", "--no-deps", "compass", *args, check=False, timeout=timeout)
        assert result.returncode == expect, f"compass {' '.join(args)} exited {result.returncode}, expected {expect}:\n{result.stderr[-2000:]}"
        if expect != 0:
            return result.stderr
        start = result.stdout.find("{")
        return json.loads(result.stdout[start:]) if start >= 0 else result.stdout

    # The install's files. On Linux, what the containers write in the bind mounts (config/,
    # extensions/, map/) belongs to root, so a test running as you cannot always read or
    # rewrite it; then the operation goes through a throwaway container, as root. On Docker
    # Desktop the direct path always works.
    def _as_root(self, script: str, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "run", "--rm", "-i", "-v", f"{self.project}:/w", "--entrypoint", "sh", IMAGES["COMPASS_IMAGE"],
                               "-c", script, "sh", *args], input=stdin, capture_output=True, text=True, timeout=120)

    def read(self, relative: str) -> Optional[str]:
        """The text of a file in the install, or None when there is none."""
        path = self.project / relative
        try:
            return path.read_text()
        except FileNotFoundError:
            return None
        except PermissionError:
            result = self._as_root('test -e "/w/$1" && cat "/w/$1"', relative)
            return result.stdout if result.returncode == 0 else None

    def write(self, relative: str, text: str) -> None:
        path = self.project / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except PermissionError:
            result = self._as_root('mkdir -p "$(dirname "/w/$1")" && cat > "/w/$1"', relative, stdin=text)
            assert result.returncode == 0, result.stderr

    def remove(self, relative: str) -> None:
        try:
            (self.project / relative).unlink(missing_ok=True)
        except PermissionError:
            self._as_root('rm -f "/w/$1"', relative)

    def exists(self, relative: str) -> bool:
        return self.read(relative) is not None

    def env(self, **values: Optional[str]) -> None:
        """Set (or, with None, remove) lines in the install's .env."""
        path = self.project / ".env"
        lines = path.read_text().splitlines()
        for key, value in values.items():
            lines = [line for line in lines if not line.startswith(f"{key}=")]
            if value is not None:
                lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n")

    def restart(self, *services: str) -> None:
        """Recreate services so they read .env again, and wait until the API answers."""
        # --no-deps: --force-recreate would otherwise recreate the services these depend on too
        # (some Compose versions do), and moto and the fakes keep the seeded world in memory
        self.compose("up", "-d", "--force-recreate", "--no-deps", *services)
        self.wait_healthy()
        self._api = None

    def wait_healthy(self, timeout: float = 240) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.web}/health", timeout=3).json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(2)
        raise AssertionError(f"Compass did not answer /health within {timeout}s")

    # the API, signed in
    def credentials(self) -> Dict[str, str]:
        login = self.cli("login")
        return {"username": login["username"], "password": login["password"]}

    def sign_in(self, client: httpx.Client) -> httpx.Client:
        client.post("/api/auth/login", json=self.credentials()).raise_for_status()
        return client

    @property
    def api(self) -> httpx.Client:
        if self._api is None:
            self._api = self.sign_in(httpx.Client(base_url=self.web, timeout=60))
        return self._api

    def anonymous(self) -> httpx.Client:
        return httpx.Client(base_url=self.web, timeout=30)

    # the fakes
    @property
    def fakes(self) -> httpx.Client:
        return httpx.Client(base_url=self.fakes_url, timeout=30)

    def aws(self) -> Any:
        import boto3

        return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name=fixture.REGION)

    def aws_client(self, service: str) -> Any:
        return self.aws().client(service, endpoint_url=self.moto_url, region_name=fixture.REGION)

    def repo(self) -> Dict[str, Any]:
        owner, name = fixture.REPO.split("/")
        return self.fakes.get(f"/_control/repos/{owner}/{name}").json()

    # jobs
    def wait_job(self, job_id: str, *, timeout: float = 600, until: tuple = ("completed", "failed")) -> Dict[str, Any]:
        deadline = time.time() + timeout
        job: Dict[str, Any] = {}
        while time.time() < deadline:
            job = self.api.get(f"/api/jobs/{job_id}").json()
            if str(job.get("status")) in until:
                return job
            time.sleep(2)
        raise AssertionError(f"job {job_id} still {job.get('status')} after {timeout}s: {job.get('events', [])[-3:]}")

    def run_job(self, captain: str, command: str, payload: Optional[Dict[str, Any]] = None, *, timeout: float = 600) -> Dict[str, Any]:
        """Start a command as a job from the API (what the page's buttons do) and wait for it."""
        started = self.api.post("/api/jobs", json={"captain": captain, "command": command, "payload": payload or {}})
        assert started.status_code == 200, started.text
        return self.wait_job(started.json()["job_id"], timeout=timeout)

    def check_world(self) -> None:
        """The seeded repository and account are still there: a test that fails on a lost world
        says so, rather than with a 404 from somewhere inside Compass."""
        owner, name = fixture.REPO.split("/")
        repo = self.fakes.get(f"/_control/repos/{owner}/{name}")
        assert repo.status_code == 200, "the fake GitHub lost its repository: was the fakes container recreated? (docker compose ps)"
        buckets = {b["Name"] for b in self.aws_client("s3").list_buckets().get("Buckets", [])}
        assert fixture.STATE_BUCKET in buckets, "moto lost the seeded account: was its container recreated? (docker compose ps)"

    def logs(self) -> str:
        """Each container's creation time (a recreated one lost its state), then the last log lines."""
        ps = self.compose("ps", "-a", "--format", "{{.Service}} {{.CreatedAt}} {{.Status}}", check=False).stdout
        return ps + "\n" + self.compose("logs", "--no-color", "--tail", "300", check=False).stdout


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():  # root's files from an earlier run's containers (Linux)
        subprocess.run(["docker", "run", "--rm", "-v", f"{path.parent}:/w", "--entrypoint", "rm", "busybox:1.36", "-rf", f"/w/{path.name}"],
                       capture_output=True, timeout=120)


def make_install(project: Path) -> Stack:
    """The quickstart's first step: an empty directory with the shipped files and an .env."""
    _remove_tree(project)
    (project / "aws").mkdir(parents=True)
    for name in ("docker-compose.yml", "nginx.conf"):
        shutil.copy(ROOT / name, project / name)
    shutil.copy(E2E / "compose.e2e.yml", project / "compose.e2e.yml")
    shutil.copy(ROOT / ".env.example", project / ".env")
    # a profile with moto's keys: Compass only ever talks to moto and its own DynamoDB
    (project / "aws" / "config").write_text(f"[default]\nregion = {fixture.REGION}\n")
    (project / "aws" / "credentials").write_text("[default]\naws_access_key_id = testing\naws_secret_access_key = testing\n")
    # the example captain as a wheel an operator dropped in (CAP-6 installs it from the page)
    _run(["python", "-m", "build", "--wheel", "--outdir", str(project / "extensions" / "wheels"), str(ROOT / "examples" / "captain-hello")], timeout=300)
    shutil.copytree(ROOT / "examples" / "captain-hello", project / "extensions" / "captain-hello-src",
                    ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
    stack = Stack(project)
    stack.env(AWS_PROFILE=None, AWS_REGION=fixture.REGION, GITHUB_TOKEN="e2e-token", COMPASS_VERSION=VERSION,
              COMPASS_PORT=str(WEB_PORT), E2E_MOTO_PORT=str(MOTO_PORT), E2E_FAKES_PORT=str(FAKES_PORT), PIP_NO_INDEX="1",
              COMPASS_UPDATER_HEALTH_SECONDS="120", **IMAGES)
    return stack


def wait_for(url: str, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=3)
            return
        except httpx.HTTPError:
            time.sleep(1)
    raise AssertionError(f"{url} did not answer within {timeout}s")


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    project = Path(os.getenv("E2E_DIR") or ROOT / "e2e" / ".work") / PROJECT
    if os.getenv("E2E_REUSE") == "1":
        # a stack kept by an earlier run (E2E_KEEP=1): attach to it as it is, leave it up
        reused = Stack(project)
        reused.wait_healthy(timeout=30)
        yield reused
        return
    if os.getenv("E2E_BUILD", "1") != "0" or not images_present():
        build_images()
    shutil.rmtree(ARTIFACTS, ignore_errors=True)  # a run's artifacts are its own failures only
    stack = make_install(project)
    stack.compose("down", "-v", "--remove-orphans", check=False)
    try:
        stack.compose("up", "-d", "--build", "moto", "fakes", "dynamodb")  # what init_config and init_tables need
        wait_for(f"{stack.moto_url}/moto-api/")
        wait_for(f"{stack.fakes_url}/health")
        # the quickstart, as written
        stack.records["init_config"] = stack.cli("init_config", "workspace=local", f"region={fixture.REGION}",
                                                 f"github_repo={fixture.REPO}", f"terraform_root={fixture.TERRAFORM_ROOT}")
        stack.records["init_tables"] = stack.cli("init_tables")
        stack.records["init_tables_again"] = stack.cli("init_tables")
        # the profile, used once: moto's static keys stand in for it (compose.e2e.yml passes them to this one run)
        stack.records["connect"] = stack.cli("connect", f"account={fixture.ACCOUNT}", f"region={fixture.REGION}", f"state_bucket={fixture.STATE_BUCKET}")
        stack.records["connect_again"] = stack.cli("connect", f"account={fixture.ACCOUNT}", f"region={fixture.REGION}")
        stack.compose("up", "-d")
        stack.wait_healthy()
        # The world is seeded once everything runs: moto and the fakes keep it in memory, and
        # some Compose versions recreate a dependency while starting another service.
        wait_for(f"{stack.moto_url}/moto-api/")
        wait_for(f"{stack.fakes_url}/health")
        stack.seeded = fixture.seed_aws(_MotoSession(stack))
        fixture.seed_github(stack.fakes)
        stack.check_world()
        yield stack
    except Exception:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "setup-logs.txt").write_text(stack.logs())
        raise
    finally:
        if os.getenv("E2E_KEEP") != "1":
            stack.compose("down", "-v", "--remove-orphans", check=False)


class _MotoSession:
    """A boto3 session whose clients all go to the suite's moto."""

    def __init__(self, stack: Stack):
        self.stack = stack

    def client(self, service: str, **kwargs: Any) -> Any:
        return self.stack.aws_client(service)


# ── the map, once ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def mapped(stack: Stack) -> Dict[str, Any]:
    """The first full scan of the seeded account, run once for every test that needs a map."""
    if stack.records.get("scan"):
        return stack.records["scan"]
    stack.check_world()
    started = stack.api.post("/api/maps/reindex", json={"mode": "full"})
    assert started.status_code == 200, started.text
    job = stack.wait_job(started.json()["job_id"], timeout=900)
    assert job["status"] == "completed", job
    stack.records["scan"] = job
    return job


# ── the browser ─────────────────────────────────────────────────────────────

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=os.getenv("E2E_HEADED") != "1")
        yield browser
        browser.close()


class Page:
    """A Playwright page signed in to the stack, recording console errors."""

    def __init__(self, page: Any, stack: Stack):
        self.page = page
        self.stack = stack
        self.console_errors: List[str] = []
        page.on("console", lambda msg: msg.type == "error" and self.console_errors.append(msg.text))
        page.on("pageerror", lambda error: self.console_errors.append(str(error)))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.page, name)

    def open(self, path: str, *, wait_for: Optional[str] = None) -> Any:
        self.page.goto(self.stack.web + path)
        self.page.wait_for_load_state("networkidle")
        if wait_for:
            self.page.get_by_text(wait_for, exact=False).first.wait_for()
        return self.page


def _new_page(browser: Any, stack: Stack, request: Any, *, signed_in: bool, viewport: Optional[Dict[str, int]] = None) -> Iterator[Page]:
    context = browser.new_context(viewport=viewport or {"width": 1440, "height": 1000})
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    if signed_in:
        client = stack.sign_in(stack.anonymous())
        context.add_cookies([{"name": c.name, "value": c.value, "url": stack.web} for c in client.cookies.jar])
    page = Page(context.new_page(), stack)
    yield page
    failed = any(getattr(getattr(request.node, f"report_{when}", None), "failed", False) for when in ("setup", "call"))
    if failed:
        where = ARTIFACTS / re.sub(r"[^\w.-]+", "_", request.node.nodeid)
        where.mkdir(parents=True, exist_ok=True)
        try:
            page.page.screenshot(path=str(where / "screenshot.png"), full_page=True)
        except Exception:  # noqa: BLE001 - the page may be gone; the trace still has it
            pass
        context.tracing.stop(path=str(where / "trace.zip"))
        (where / "console.txt").write_text("\n".join(page.console_errors))
        (where / "stack-logs.txt").write_text(stack.logs())
    else:
        context.tracing.stop()
    context.close()


@pytest.fixture
def page(browser: Any, stack: Stack, request: Any) -> Iterator[Page]:
    yield from _new_page(browser, stack, request, signed_in=True)


@pytest.fixture
def anonymous_page(browser: Any, stack: Stack, request: Any) -> Iterator[Page]:
    yield from _new_page(browser, stack, request, signed_in=False)


@pytest.fixture
def phone_page(browser: Any, stack: Stack, request: Any) -> Iterator[Page]:
    yield from _new_page(browser, stack, request, signed_in=True, viewport={"width": 390, "height": 844})


@pytest.fixture(autouse=True)
def _logs_on_failure(request: Any) -> Iterator[None]:
    """API and CLI tests have no browser; a failure still leaves the stack's logs."""
    yield
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed and "stack" in request.fixturenames and "page" not in request.fixturenames:
        where = ARTIFACTS / re.sub(r"[^\w.-]+", "_", request.node.nodeid)
        where.mkdir(parents=True, exist_ok=True)
        (where / "stack-logs.txt").write_text(request.getfixturevalue("stack").logs())
