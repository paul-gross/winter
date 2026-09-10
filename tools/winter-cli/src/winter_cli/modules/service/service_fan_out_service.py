"""Fan-out orchestration for `up` and `down` across a matrix of (provider, scope) cells.

``ServiceFanOutService`` implements:

- **Forward fan-out (up):** Iterates cells in the order supplied (the deterministic
  order ``ServiceStatusMatrixService.build_matrix`` returns — env cells sorted by
  env name then provider order, followed by workspace cells — for stable output
  only, no ordering semantics). Runs each cell's provider ``up <positional>``
  action, where ``positional`` is the bare scope (``"alpha"``) when the cell
  carries no service-segment filter, or the scope-qualified pattern
  (``"alpha/api"``) when the user supplied a real filter for that scope. Aborts
  on the first non-zero exit code and returns it; subsequent cells are not
  started.

- **Best-effort fan-out (down):** Iterates cells in the same deterministic order.
  Runs each cell's provider ``down <positional>`` action. Continues past
  failures; returns the first non-zero exit code (0 if all succeeded).

No readiness gate, no status polling, no inter-cell ordering semantics.

Per-scope env injection
------------------------
Each cell's provider subprocess environment is provisioned once per unique scope
(``WINTER_ENV``/``WINTER_ENV_INDEX``/``WINTER_PORT_BASE``/``WINTER_WORKSPACE_PORT_BASE``
plus env-band vars), exactly as the status matrix injects env per cell, and cached
so a scope shared by multiple cells (multi-provider) is only computed once.

``up`` provisions with ``resolve_commands=True`` — services need real values,
so a command entry runs (at most once per unique scope, per the cache above).
``down`` provisions with ``resolve_commands=False`` — teardown needs ports and
names only, so a command entry is never run.

Extension-declared service manifests
-------------------------------------
``manifest_collector`` is an optional factory (a ``ServiceManifestCollectorService``
or any callable returning a ``CollectedManifest``).  On ``up``, it is called once and
the resulting ``env_additions()`` dict (``WINTER_SERVICE_MANIFEST=<path>``) is merged
into every cell's provider subprocess environment.  ``down`` never calls the collector —
it shuts providers down without aggregating manifests.  Providers that understand the
contract read the TOML file and merge the extension-declared services into their live
configuration; providers that predate the contract ignore the env var.

Effective wait timeout
-----------------------
``up`` also injects ``WINTER_SERVICE_TIMEOUT`` — the effective ``winter service up
--wait --timeout`` value (core's ``timeout_s``, always the winter-side default or the
user-supplied override, regardless of whether ``--wait`` was passed) — as a plain
float string into every cell's provider subprocess environment, so providers can honor
the caller's timeout in their own up-time readiness gates (e.g. ``depends_on``).
``down`` never injects it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.provider_invocation import (
    IEnvProvisioner,
    apply_provisioned_env,
    build_provider_env,
    provision_scope_env,
)
from winter_cli.modules.service.service_readiness_service import DEFAULT_WAIT_TIMEOUT_S

if TYPE_CHECKING:
    from winter_cli.modules.service.service_manifest_collector import ServiceManifestCollectorService
    from winter_cli.modules.service.service_reporter import IServiceReporter

# The env-var name handed to each provider subprocess on `up`, carrying the
# effective `winter service up --wait --timeout` value (core's `timeout_s`).
WINTER_SERVICE_TIMEOUT_ENV = "WINTER_SERVICE_TIMEOUT"


@dataclasses.dataclass(frozen=True)
class FanOutCell:
    """One (provider, scope) cell to dispatch ``up``/``down`` to.

    ``scope`` is the env name (e.g. ``"alpha"``) or the literal string
    ``"workspace"`` — used to provision that scope's env once and cache it
    across cells sharing the same scope. ``positional`` is the single argv
    token forwarded to the provider: the bare scope when the cell carries no
    service-segment filter, or the scope-qualified pattern (``"alpha/api"``)
    when the user supplied a real filter for that scope.
    """

    provider: ResolvedCapability
    scope: str
    positional: str


class ServiceFanOutService:
    """Orchestrates ``up``/``down`` across an ordered list of matrix cells.

    ``up`` fans out forward, aborting on the first cell failure.
    ``down`` fans out in the same order, best-effort (continues past failures).

    ``manifest_collector`` is an optional ``ServiceManifestCollectorService``
    (or any object with a ``collect()`` method returning a ``CollectedManifest``).
    On ``up``, ``collect()`` is called once and the result's ``env_additions()``
    dict is merged into every cell's provider subprocess env so the provider sees
    ``WINTER_SERVICE_MANIFEST=<path>``.  ``down`` skips collection entirely —
    providers are stopped without manifest aggregation.

    ``env_provisioner`` is an optional env provisioner (the ``IEnvProvisioner``
    protocol — any object with a ``compute(scope, *, resolve_commands)``
    method).  When present, ``compute(cell.scope, resolve_commands=...)`` is
    called (once per unique scope, cached) for ``up`` and ``down`` to inject
    ``WINTER_ENV``, ``WINTER_ENV_INDEX``, ``WINTER_PORT_BASE``,
    ``WINTER_WORKSPACE_PORT_BASE``, and any env-band variables into the
    provider subprocess environment. This is the runtime-injection model: env
    vars flow via the provider subprocess environment rather than through any
    on-disk file. ``up`` passes ``resolve_commands=True`` (services need real
    values); ``down`` passes ``resolve_commands=False`` (teardown needs ports
    and names only).
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        workspace_root: Path,
        service_prefix: str,
        manifest_collector: ServiceManifestCollectorService | None = None,
        env_provisioner: IEnvProvisioner | None = None,
        reporter: IServiceReporter | None = None,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._workspace_root = workspace_root
        self._service_prefix = service_prefix
        self._manifest_collector = manifest_collector
        self._env_provisioner = env_provisioner
        self._reporter = reporter

    # ── public interface ──────────────────────────────────────────────────────

    def up(self, cells: list[FanOutCell], timeout_s: float = DEFAULT_WAIT_TIMEOUT_S) -> int:
        """Start every cell's provider in forward order.

        Injects the computed env trio (``WINTER_ENV``, ``WINTER_ENV_INDEX``,
        ``WINTER_PORT_BASE``, ``WINTER_WORKSPACE_PORT_BASE``, and any
        env-band entries) into every cell's provider subprocess environment,
        provisioned once per unique scope. Collects the extension-declared
        service manifest once (lazily) and injects ``WINTER_SERVICE_MANIFEST``
        on top, alongside ``WINTER_SERVICE_TIMEOUT`` (``timeout_s`` as a plain
        float string) so providers can honor the caller's effective
        ``--timeout`` in their own up-time readiness gates.
        Returns 0 on full success. Returns the first non-zero exit code on
        cell failure, without dispatching subsequent cells.
        """
        provisioned_cache: dict[str, dict[str, str]] = {}
        extra_env = {**self._collect_manifest_env(), WINTER_SERVICE_TIMEOUT_ENV: str(timeout_s)}
        for cell in cells:
            provisioned = self._provisioned_for(cell.scope, provisioned_cache, resolve_commands=True)
            exit_code = self._run_action(cell, "up", provisioned, extra_env)
            if exit_code != 0:
                return exit_code
        return 0

    def down(self, cells: list[FanOutCell]) -> int:
        """Stop every cell's provider in forward order, best-effort.

        Injects the computed env trio into every cell's provider subprocess
        environment (provisioned once per unique scope) so providers can
        cleanly stop scope-specific resources without needing to source the
        env file themselves.
        Does NOT collect the service manifest — ``down`` shuts providers down
        without aggregating extension-declared service definitions.
        Continues past failures; returns the first non-zero exit code (or 0 if
        all succeeded).
        """
        provisioned_cache: dict[str, dict[str, str]] = {}
        first_error: int = 0
        for cell in cells:
            provisioned = self._provisioned_for(cell.scope, provisioned_cache, resolve_commands=False)
            exit_code = self._run_action(cell, "down", provisioned, {})
            if exit_code != 0 and first_error == 0:
                first_error = exit_code
        return first_error

    # ── internals ────────────────────────────────────────────────────────────

    def _provisioned_for(
        self, scope: str, cache: dict[str, dict[str, str]], *, resolve_commands: bool
    ) -> dict[str, str]:
        """Return the computed env map for *scope*, computing and caching it once.

        *resolve_commands* is the caller's gate value (``True`` for ``up``,
        ``False`` for ``down``) — a command entry therefore runs at most once
        per unique scope per ``up()`` call, never once per cell.
        """
        if scope not in cache:
            cache[scope] = provision_scope_env(
                self._env_provisioner, scope, self._reporter, resolve_commands=resolve_commands
            )
        return cache[scope]

    def _collect_manifest_env(self) -> dict[str, str]:
        """Invoke the manifest collector and return env-var additions for ``up``.

        Returns an empty dict when no collector is configured or when the
        collector finds no extension-declared service definitions.
        """
        if self._manifest_collector is None:
            return {}
        collected = self._manifest_collector.collect()
        return collected.env_additions()

    def _run_action(
        self,
        cell: FanOutCell,
        action: str,
        provisioned_env: dict[str, str],
        extra_env: dict[str, str],
    ) -> int:
        cmd = [str(cell.provider.entrypoint), action, cell.positional]
        merged = build_provider_env(cell.provider, self._workspace_root, self._service_prefix)
        merged = apply_provisioned_env(merged, provisioned_env)
        if extra_env:
            merged = {**merged, **extra_env}
        return self._subprocess_runner.call(cmd, cwd=self._workspace_root, env=merged)
