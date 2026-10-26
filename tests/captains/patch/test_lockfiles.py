"""Lockfiles (docs/specs/patching-captain.md open decision 1, "ci"): the plan finds the lockfile beside a
moved manifest, the workflow regenerates it with the ecosystem's tool and commits as Compass, and
manifest-only mode leaves it to the body."""

from __future__ import annotations

from captains.patch import lockfiles


def test_the_plan_finds_the_lockfile_beside_each_moved_manifest():
    present = {"web/composer.lock", "pnpm-lock.yaml", "svc/go.sum", "py/uv.lock"}
    edits = {
        "web/composer.json": [{"package": "laravel/framework", "fixed_version": "11.2.1"}, {"package": "symfony/http-foundation", "fixed_version": "6.4.1"}],
        "package.json": [{"package": "express", "fixed_version": "4.19.2"}],
        "svc/go.mod": [{"package": "github.com/gin-gonic/gin", "fixed_version": "v1.10.0"}],
        "py/pyproject.toml": [{"package": "fastapi", "fixed_version": "0.115.1"}],
        "lonely/Cargo.toml": [{"package": "tokio", "fixed_version": "1.38.1"}],
    }
    entries = lockfiles.plan(edits, lambda path: path in present, known_lockfiles={"web/composer.lock"})
    assert [(e.manifest, e.lockfile, e.directory, e.tool) for e in entries] == [
        ("web/composer.json", "web/composer.lock", "web", "composer"), ("package.json", "pnpm-lock.yaml", ".", "pnpm"),
        ("svc/go.mod", "svc/go.sum", "svc", "go"), ("py/pyproject.toml", "py/uv.lock", "py", "uv")]
    assert entries[0].packages == ("laravel/framework@11.2.1", "symfony/http-foundation@6.4.1")
    assert lockfiles.mode({}) == "ci" and lockfiles.mode({"patch": {"lockfiles": "manifest-only"}}) == "manifest-only" and lockfiles.mode({"patch": {"lockfiles": "weird"}}) == "ci"


def test_the_workflow_regenerates_each_lockfile_with_its_tool_and_commits_as_compass():
    entries = lockfiles.plan({"web/composer.json": [{"package": "laravel/framework", "fixed_version": "11.2.1"}], "Cargo.toml": [{"package": "tokio", "fixed_version": "1.38.1"}]},
                             lambda path: path in {"web/composer.lock", "Cargo.lock"})
    text = lockfiles.workflow(entries, author_email="compass@acme.example")
    assert text.startswith("# Added by Compass") and "name: compass-lockfile" in text and 'paths:\n      - "Cargo.toml"\n      - "web/composer.json"' in text
    assert "token: ${{ secrets.COMPASS_LOCKFILE_TOKEN || secrets.GITHUB_TOKEN }}" in text and "ref: ${{ github.head_ref }}" in text
    assert 'git config user.email "compass@acme.example"' in text and "permissions:\n  contents: write" in text
    assert "composer update ${{ matrix.packages }} --with-all-dependencies --no-install --no-scripts" in text and 'packages: "laravel/framework@11.2.1"' in text
    assert "cargo update -p" in text and "dtolnay/rust-toolchain@stable" in text and "shivammathur/setup-php@v2" in text
    assert text.count("runs-on: ubuntu-latest") == 2 and "lock_cargo_lock:" in text and "lock_web_composer_lock:" in text
    assert lockfiles.note(entries[0], "ci") == "web/composer.lock is regenerated on this branch by .github/workflows/compass-lockfile.yml (composer)"
    assert lockfiles.note(None, "ci") == "no lockfile beside the manifest; nothing to regenerate"
    assert lockfiles.note(entries[0], "manifest-only").startswith("the lockfile needs regenerating")
    assert lockfiles.author_email({"github": {"author_email": "bot@acme.example"}}) == "bot@acme.example"
