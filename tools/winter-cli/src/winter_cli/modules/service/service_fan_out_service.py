"""Fan-out orchestration for `up` and `down` across an ordered provider list.

``ServiceFanOutService`` implements:

- **Forward fan-out (up):** Iterates providers in the order ``resolve_all`` returns
  them (deterministic, for stable output only — no ordering semantics). Runs each
  provider's ``up <env>`` action. Aborts on the first non-zero exit code and returns
  it; subsequent providers are not started.

- **Best-effort fan-out (down):** Iterates providers in the same deterministic order.
  Runs each provider's ``down <env>`` action. Continues past failures; returns the
  first non-zero exit code (0 if all succeeded).

No readiness gate, no status polling, no inter-provider ordering semantics.
"""

from __future__ import annotations

from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.provider_invocation import build_provider_env


class ServiceFanOutService:
    """Orchestrates ``up``/``down`` across an ordered list of providers.

    ``up`` fans out forward, aborting on the first provider failure.
    ``down`` fans out in the same order, best-effort (continues past failures).
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        workspace_root: Path,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._workspace_root = workspace_root

    # ── public interface ──────────────────────────────────────────────────────

    def up(self, env: str, providers: list[ResolvedCapability]) -> int:
        """Start all providers in forward order.

        Returns 0 on full success. Returns the first non-zero exit code on
        provider failure, without starting subsequent providers.
        """
        for provider in providers:
            exit_code = self._run_action(provider, "up", [env])
            if exit_code != 0:
                return exit_code
        return 0

    def down(self, env: str, providers: list[ResolvedCapability]) -> int:
        """Stop all providers in forward order, best-effort.

        Continues past failures; returns the first non-zero exit code (or 0 if
        all succeeded).
        """
        first_error: int = 0
        for provider in providers:
            exit_code = self._run_action(provider, "down", [env])
            if exit_code != 0 and first_error == 0:
                first_error = exit_code
        return first_error

    # ── internals ────────────────────────────────────────────────────────────

    def _run_action(self, provider: ResolvedCapability, action: str, positionals: list[str]) -> int:
        cmd = [str(provider.entrypoint), action, *positionals]
        merged = build_provider_env(provider, self._workspace_root)
        return self._subprocess_runner.call(cmd, cwd=self._workspace_root, env=merged)
