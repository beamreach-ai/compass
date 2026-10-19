"""4.12 Release artefacts (docs/e2e.md): the images start, the changelog has the release,
the quickstart's commands are the ones this suite ran."""

from __future__ import annotations

import re
import subprocess

from e2e.conftest import IMAGES, ROOT, VERSION


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=300)


def test_rel_1_the_three_images_start(stack):
    compass = _run("docker", "run", "--rm", IMAGES["COMPASS_IMAGE"], "--help")
    assert compass.returncode == 0 and "compass" in compass.stdout.lower(), compass.stderr
    version = _run("docker", "run", "--rm", "--entrypoint", "python", IMAGES["COMPASS_IMAGE"], "-c",
                   "import compass_core; print(compass_core.__version__)")
    assert version.stdout.strip() == VERSION
    updater = _run("docker", "run", "--rm", "--entrypoint", "sh", IMAGES["COMPASS_UPDATER_IMAGE"], "-c",
                   "docker compose version && jq --version && curl --version | head -1 && sh -n /usr/local/bin/compass-updater")
    assert updater.returncode == 0, updater.stderr
    services = stack.compose("ps", "--format", "{{.Service}} {{.Image}}").stdout
    assert IMAGES["COMPASS_DASHBOARD_IMAGE"] in services and IMAGES["COMPASS_UPDATER_IMAGE"] in services


def test_rel_2_the_changelog_has_this_release_and_the_feed_script_reads_it():
    notes = _run("python", str(ROOT / ".github" / "scripts" / "release_feed.py"), "notes", VERSION, "--changelog", str(ROOT / "CHANGELOG.md"))
    assert notes.returncode == 0 and notes.stdout.strip(), notes.stderr
    missing = _run("python", str(ROOT / ".github" / "scripts" / "release_feed.py"), "notes", "99.0.0", "--changelog", str(ROOT / "CHANGELOG.md"))
    assert missing.returncode == 1 and "part of a release" in missing.stderr


def test_rel_3_the_quickstart_runs_the_commands_this_suite_ran(stack):
    quickstart = (ROOT / "docs" / "quickstart.md").read_text()
    readme = (ROOT / "README.md").read_text()
    for text in (quickstart, readme):
        assert re.search(r"docker compose run --rm compass init_config workspace=local region=\S+ \\\n\s+github_repo=", text)
        assert "docker compose run --rm compass init_tables" in text and "docker compose up -d" in text
    for name in ("docker-compose.yml", "nginx.conf", ".env.example"):
        assert f"{name}" in quickstart.split("## 2.")[0], f"the quickstart downloads {name}"
        assert (ROOT / name).exists()
    assert {"init_config", "init_tables"} <= set(stack.records), "the harness ran them"
