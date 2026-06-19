from __future__ import annotations

import os
from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.spec_loader import ISpecLoader
from winter_cli.modules.capability.spec_models import ArityKind, CheckKind
from winter_cli.modules.ext.models import CheckResult, VerifyReport
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver

# Probe token used for accepts-action checks: a recognisable env name that no
# real orchestrator would treat as a real env, so a real orchestrator exits non-2.
_PROBE_ENV = "__winter_verify_env__"

# Probe pattern used for patterns_optional / patterns_required arity.
_PROBE_PATTERN = "__winter_verify/probe__"

# Sentinel used by forwards-params checks: must appear in stdout/stderr when
# the orchestrator echoes its argv back.
_SENTINEL = "__WINTER_VERIFY_SENTINEL__"

# Unknown action used for refuses-unknown checks.
_UNKNOWN_ACTION = "__winter_nonexistent_action__"

# Exit code that means "unknown action" in the service/v1 spec.
_UNKNOWN_ACTION_EXIT = 2


class ConformanceVerifyService:
    """Runs golden invocations from a capability spec against an extension entrypoint.

    Resolution accepts a bare registered name OR a local path (same semantics as
    `--service-orchestrator`): if the value contains a path separator or names an
    existing directory, it is treated as a local path; otherwise it is looked up
    among the installed standalone repos.

    Delegates to `ServiceOrchestratorResolver.try_resolve_extension` so resolution
    logic lives in one place — no private fork of the path/name disambiguation here.

    Setup failures (dir missing, no manifest, no entrypoint declared, entrypoint
    file missing) are surfaced as a `VerifyReport` with `setup_failure` set; no
    checks are run in that case.
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        orchestrator_resolver: ServiceOrchestratorResolver,
        spec_loader: ISpecLoader,
        workspace_root: Path,
    ) -> None:
        self._runner = subprocess_runner
        self._orchestrator_resolver = orchestrator_resolver
        self._spec_loader = spec_loader
        self._workspace_root = workspace_root

    def verify(self, extension: str) -> VerifyReport:
        """Run all spec checks against `extension` and return a `VerifyReport`."""
        resolved = self._orchestrator_resolver.try_resolve_extension(extension)
        if isinstance(resolved, str):
            return VerifyReport(setup_failure=resolved)

        entrypoint = resolved.entrypoint
        ext_dir = resolved.ext_dir
        prefix = resolved.prefix

        spec = self._spec_loader.load("service", self._pick_version())
        env = self._make_env(ext_dir, prefix, spec.env_vars)

        results: list[CheckResult] = []
        for check in spec.checks:
            if check.kind == CheckKind.accepts_action:
                for action in spec.actions:
                    probe_args = self._probe_args(action.arity)
                    argv = [str(entrypoint), action.name, *probe_args]
                    result = self._runner.run(argv, cwd=self._workspace_root, env=env)
                    passed = result.returncode != _UNKNOWN_ACTION_EXIT
                    if passed:
                        detail = f"accepts action '{action.name}' (exit {result.returncode})"
                    else:
                        detail = (
                            f"action '{action.name}' returned exit {result.returncode} "
                            f"(unknown-action signal) — expected exit != {_UNKNOWN_ACTION_EXIT}"
                        )
                    results.append(
                        CheckResult(
                            check_id=f"accepts-{action.name}",
                            passed=passed,
                            detail=detail,
                            argv=argv,
                            observed_exit=result.returncode,
                        )
                    )

            elif check.kind == CheckKind.refuses_unknown:
                argv = [str(entrypoint), _UNKNOWN_ACTION]
                result = self._runner.run(argv, cwd=self._workspace_root, env=env)
                passed = result.returncode != 0
                if passed:
                    detail = f"refuses unknown action (exit {result.returncode})"
                else:
                    detail = "accepts unknown action — got exit 0, expected non-zero"
                results.append(
                    CheckResult(
                        check_id="refuses-unknown",
                        passed=passed,
                        detail=detail,
                        argv=argv,
                        observed_exit=result.returncode,
                    )
                )

            elif check.kind == CheckKind.forwards_params:
                # Use the status action (patterns-optional, no required positionals) to
                # pass the sentinel as the pattern token — the entrypoint should echo it
                # back on stdout or stderr when forwarding argv.
                argv = [str(entrypoint), "status", f"{_SENTINEL}/__svc__"]
                result = self._runner.run(argv, cwd=self._workspace_root, env=env)
                combined = result.stdout + result.stderr
                passed = _SENTINEL in combined
                if passed:
                    detail = "sentinel appears in output (argv forwarded)"
                else:
                    detail = (
                        f"sentinel '{_SENTINEL}' not found in stdout/stderr — "
                        "entrypoint may not echo argv"
                    )
                results.append(
                    CheckResult(
                        check_id="forwards-params",
                        passed=passed,
                        detail=detail,
                        argv=argv,
                        observed_exit=result.returncode,
                    )
                )

        return VerifyReport(results=results)

    def _pick_version(self) -> str:
        """Pick the highest available version for the service spec."""
        versions = sorted(self._spec_loader.supported_versions("service"))
        if not versions:
            raise RuntimeError("No service spec versions found in the bundled specs directory.")
        return versions[-1]

    @staticmethod
    def _probe_args(arity: ArityKind) -> list[str]:
        """Return the probe positional args to pass alongside an action name."""
        if arity == ArityKind.single_env:
            return [_PROBE_ENV]
        if arity == ArityKind.patterns_required:
            return [_PROBE_PATTERN]
        # patterns_optional — zero args is valid
        return []

    def _make_env(self, ext_dir: Path, prefix: str, spec_env_vars: tuple) -> dict[str, str]:
        """Build the subprocess environment from the spec's declared always-present env vars.

        The spec's ``env_vars`` tuple is the single source of truth for which
        variables winter sets on every dispatch — sourced from the same spec that
        drives verification, so a v2 spec that adds or renames an always-present var
        automatically updates what the verifier probes.
        """
        # Map the three known env var names to their values.  Any name declared in
        # the spec that has no mapping here is silently skipped — the verifier only
        # asserts on what it can observe (argv sentinel), not the env vars themselves.
        known_values: dict[str, str] = {
            "WINTER_WORKSPACE_DIR": str(self._workspace_root),
            "WINTER_EXT_DIR": str(ext_dir),
            "WINTER_EXT_PREFIX": prefix,
        }
        merged = os.environ.copy()
        for ev in spec_env_vars:
            if ev.name in known_values:
                merged[ev.name] = known_values[ev.name]
        return merged
