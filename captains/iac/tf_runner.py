"""Thin wrapper around the terraform CLI.

Every terraform invocation the import pipeline makes goes through here so that
credentials, timeouts and the plugin cache are configured in exactly one place,
and so the rest of the pipeline can be tested by faking this object rather than
by shelling out.

Two rules hold throughout:

* every invocation is read-only. `plan` refreshes state but never writes it, and
  is always passed `-lock=false` so a demo run cannot contend with a real apply
  for the DynamoDB lock.
* nothing here ever runs `apply`. There is deliberately no method for it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("iac-tf-runner")

DEFAULT_TIMEOUT_S = 300


class TerraformUnavailable(RuntimeError):
    """The terraform binary is missing or too old."""


class TerraformError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args_run = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout or "").strip()
        super().__init__(f"terraform {' '.join(args)} failed ({returncode}): {detail[:2000]}")


@dataclass
class PlanSummary:
    """The parts of a plan the verify gate cares about."""

    imports: List[str]
    creates: List[str]
    updates: List[str]
    destroys: List[str]
    raw: Dict[str, Any]

    @property
    def is_import_only(self) -> bool:
        return not (self.creates or self.updates or self.destroys)

    def minus(self, baseline: "PlanSummary") -> "PlanSummary":
        """This plan with the baseline's changes removed.

        A repository whose main branch already drifts from reality plans dirty
        before anyone touches it — `beamreach-demo-infra` plans 3 to add, 2 to
        change and 6 to destroy on an untouched checkout. Judging a pull request
        against zero therefore blames it for the state of the repository it
        arrived in, and every import gets labelled "needs review" for reasons
        that have nothing to do with it.

        Subtracting a plan of the base branch leaves only what this change
        actually introduces, which is the only thing a reviewer can act on.
        """
        seen_creates = set(baseline.creates)
        seen_updates = set(baseline.updates)
        seen_destroys = set(baseline.destroys)
        return PlanSummary(
            imports=list(self.imports),
            creates=[a for a in self.creates if a not in seen_creates],
            updates=[a for a in self.updates if a not in seen_updates],
            destroys=[a for a in self.destroys if a not in seen_destroys],
            raw=self.raw,
        )


class TerraformRunner:
    def __init__(
        self,
        binary: str = "terraform",
        plugin_cache_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.binary = binary
        self.timeout_s = timeout_s
        self._env = dict(os.environ)
        for key, value in (env or {}).items():
            # A None value *removes* the variable. Needed for AWS_PROFILE: the
            # worker sets one for the control-plane account, and an inherited
            # profile takes precedence over explicit credentials, so overriding
            # the key/secret alone would not be enough to retarget terraform.
            if value is None:
                self._env.pop(key, None)
            else:
                self._env[key] = value
        # A warm plugin cache is what keeps `init` from reaching the registry on
        # every job; without it each import pays a provider download.
        cache = plugin_cache_dir or os.getenv("TF_PLUGIN_CACHE_DIR")
        if cache:
            Path(cache).mkdir(parents=True, exist_ok=True)
            self._env["TF_PLUGIN_CACHE_DIR"] = cache
        self._env.setdefault("TF_IN_AUTOMATION", "1")
        self._env.setdefault("TF_INPUT", "0")
        self._env.setdefault("CHECKPOINT_DISABLE", "1")

    # ------------------------------------------------------------------ utils

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def require(self) -> None:
        if not self.available():
            raise TerraformUnavailable(
                f"terraform binary '{self.binary}' not found; the import pipeline "
                "falls back to import-blocks-only when this happens"
            )

    def _run(self, args: Sequence[str], cwd: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = [self.binary, *args]
        logger.info("terraform %s (cwd=%s)", " ".join(args), cwd)
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=self._env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        return proc

    def _run_checked(self, args: Sequence[str], cwd: str, stdin: Optional[str] = None) -> str:
        proc = self._run(args, cwd, stdin)
        if proc.returncode != 0:
            raise TerraformError(args, proc.returncode, proc.stdout, proc.stderr)
        return proc.stdout

    # --------------------------------------------------------------- commands

    def init(self, cwd: str, *, backend: bool = True) -> None:
        """`-backend=false` is what makes scratch-directory generation safe.

        Without a backend there is no state and no lock table, so a generation
        run cannot touch the environment it is describing.
        """
        args = ["init", "-no-color", "-input=false"]
        if not backend:
            args.append("-backend=false")
        self._run_checked(args, cwd)

    def generate_config(self, cwd: str, out_file: str = "generated.tf") -> str:
        """Terraform's own config generator.

        Exit code 1 with the config still written is normal here: the plan
        cannot succeed because the resources have no configuration yet — that is
        precisely what it is being asked to produce. Judge success by whether
        the file exists and is non-empty, not by the return code.
        """
        args = ["plan", "-no-color", "-input=false", "-lock=false", f"-generate-config-out={out_file}"]
        proc = self._run(args, cwd)
        produced = Path(cwd) / out_file
        if produced.exists() and produced.read_text().strip():
            return produced.read_text()
        raise TerraformError(args, proc.returncode, proc.stdout, proc.stderr)

    def show_state_json(self, cwd: str) -> Dict[str, Any]:
        out = self._run_checked(["show", "-json", "-no-color"], cwd)
        return json.loads(out or "{}")

    def validate(self, cwd: str) -> List[Dict[str, Any]]:
        """Diagnostics from `terraform validate`, with file and line.

        Returns error diagnostics only. A non-zero exit is expected when the
        configuration is invalid — that is the answer, not a failure — so the
        return code is ignored in favour of the parsed payload.
        """
        proc = self._run(["validate", "-json", "-no-color"], cwd)
        try:
            payload = json.loads(proc.stdout or "{}")
        except ValueError:
            logger.warning("could not parse terraform validate output")
            return []
        return [d for d in (payload.get("diagnostics") or []) if d.get("severity") == "error"]

    def providers_schema(self, cwd: str) -> Dict[str, Any]:
        out = self._run_checked(["providers", "schema", "-json"], cwd)
        return json.loads(out or "{}")

    PROBE_FILENAME = "__beamreach_probe.tf"

    def probe_expressions(self, cwd: str, expressions: Sequence[str]) -> Dict[str, Any]:
        """Evaluate many expressions in one deterministic invocation.

        `terraform console` is the obvious tool and does not work here. Driven
        from a pipe it drops results — three expressions in, one line out, and
        the line belonged to the last of them — because early stdin is consumed
        before the REPL is ready. Batching against that is unsafe at any size:
        results are positional, so a dropped line silently pairs a value with
        the wrong expression. Wrapping in `jsonencode` to force one line each
        does not help, and collapsing everything into a single composite
        `jsonencode({...})` fails differently: one unknown member renders the
        whole object `(known after apply)`.

        So evaluate through the plan graph instead. Each expression becomes a
        temporary output, and the plan's structured JSON reports each one by
        name. Names, not positions — nothing can be silently mispaired.

        `-refresh=false` keeps this offline: values come from state, so no AWS
        API calls are made and the step cannot be slowed or broken by provider
        credentials. Outputs are declared `sensitive` so that referencing a
        module's sensitive output does not fail the plan; scrubbing of anything
        that reaches disk happens separately.
        """
        if not expressions:
            return {}

        names = {f"br_probe_{i}": expr for i, expr in enumerate(expressions)}
        body = "\n".join(
            f'output "{name}" {{\n  value     = try({expr}, null)\n  sensitive = true\n}}\n'
            for name, expr in names.items()
        )
        probe_path = Path(cwd) / self.PROBE_FILENAME
        plan_path = "__beamreach_probe.plan"

        try:
            probe_path.write_text(
                "# Generated by Beamreach to evaluate expressions; removed automatically.\n" + body,
                encoding="utf-8",
            )
            self._run_checked(
                ["plan", "-no-color", "-input=false", "-lock=false", "-refresh=false", f"-out={plan_path}"],
                cwd,
            )
            raw = json.loads(self._run_checked(["show", "-json", "-no-color", plan_path], cwd) or "{}")
        except (TerraformError, OSError, ValueError) as exc:
            logger.warning("probe plan failed, no references will be resolved: %s", exc)
            return {}
        finally:
            probe_path.unlink(missing_ok=True)
            (Path(cwd) / plan_path).unlink(missing_ok=True)

        changes = raw.get("output_changes") or {}
        resolved: Dict[str, Any] = {}
        for name, expr in names.items():
            change = changes.get(name) or {}
            if change.get("after_unknown"):
                continue
            after = change.get("after")
            if after is not None:
                resolved[expr] = after
        logger.info("probe resolved %d of %d expressions", len(resolved), len(expressions))
        return resolved

    def plan(self, cwd: str) -> PlanSummary:
        """A read-only plan, parsed into the shape the verify gate needs."""
        self._run_checked(["plan", "-no-color", "-input=false", "-lock=false", "-out=tfplan.binary"], cwd)
        raw = json.loads(self._run_checked(["show", "-json", "-no-color", "tfplan.binary"], cwd) or "{}")
        return summarise_plan(raw)


def _console_scalar(raw: str) -> Optional[str]:
    """Normalise one console result line into a comparable scalar.

    Console quotes strings and renders errors and complex values in forms that
    are not usable as index keys; anything that is not a plain scalar becomes
    None so it is simply left out of the index.
    """
    value = raw.strip()
    if not value or value.startswith("╷") or value.startswith("Error"):
        return None
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    if value in {"true", "false", "null"} or value.startswith(("[", "{", "(")):
        return None
    return value


def summarise_plan(raw: Dict[str, Any]) -> PlanSummary:
    """Split a plan's resource changes into the four categories that matter.

    `import` is reported by terraform as an `importing` key alongside the
    actions, so a resource being imported with no other change shows up as
    actions == ["no-op"] and is not a create.
    """
    imports: List[str] = []
    creates: List[str] = []
    updates: List[str] = []
    destroys: List[str] = []

    for change in raw.get("resource_changes") or []:
        addr = change.get("address", "?")
        detail = change.get("change") or {}
        actions = detail.get("actions") or []
        if detail.get("importing"):
            imports.append(addr)
        if "create" in actions and "delete" in actions:
            destroys.append(addr)  # replacement — destructive, treat as such
        elif "create" in actions:
            creates.append(addr)
        elif "delete" in actions:
            destroys.append(addr)
        elif "update" in actions:
            updates.append(addr)

    return PlanSummary(imports=imports, creates=creates, updates=updates, destroys=destroys, raw=raw)
