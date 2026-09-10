"""Call-matrix enumeration, env injection, and parallel invocation for ``service status``.

``ServiceStatusMatrixService`` is the core of winter#109 Phase 2: it replaces the
per-provider single-call approach in ``ServiceStatusService`` with a two-dimensional
call-matrix where **rows are scopes** (configured env names from the registry + the
workspace scope) and **columns are owning providers**.

Design
------
The matrix is built by joining:

(a) Each provider's ``describe`` taxonomy — ``workspace/<svc>`` entries signal that
    the provider owns a workspace-scoped service, ``*/<svc>`` entries signal a
    per-env service.  A provider that returns no ``workspace/`` names produces no
    workspace cell; a provider that returns no ``*/`` names produces no env cells.

(b) The configured-env registry (``IEnvIndexRegistry.all_assignments()``).

Single-provider short-circuit
------------------------------
When exactly one provider is bound, ``describe`` is skipped (it would convey no
ownership information) and two cells are built unconditionally: one per configured
env and one workspace cell.  This mirrors the existing D1 short-circuit in
``ServiceProviderIndex.build``.  The workspace cell is always included for the sole
provider because it may own workspace services, and an empty document from the
provider is valid and fine.

Cell rules
----------
- For each provider owning ``*/`` services x each configured env  -> ENV cell.
- For each provider owning ``workspace/`` services               -> WORKSPACE cell.
- Single-provider: one ENV cell per configured env + one WORKSPACE cell.

Scope filtering
---------------
A scope-qualified pattern (``gamma/web``) narrows the matrix to the scope axis
(only the matching env/workspace produces cells) AND the provider axis (only owning
providers produce cells for that scope).  A bare env pattern (``gamma``) narrows to
that env's cells.  No patterns → the full matrix.

Per-cell env injection
----------------------
Each cell's provider subprocess environment is built as:

    build_provider_env(provider, ws_root, service_prefix)  # WINTER_WORKSPACE_DIR / EXT_* / SERVICE_PREFIX vars
    | EnvProvisionerService.compute(scope, resolve_commands=False)  # WINTER_ENV / INDEX / PORT_BASE /
                                                                    # WINTER_WORKSPACE_PORT_BASE + env-band vars

The provisioner is the single source of truth for all WINTER_* variables.  Each
scope's env is computed at most once per matrix run (cache).  Status always
passes ``resolve_commands=False`` — health probing is provider-side and a
command entry's real value is never needed here; masking to the placeholder
also keeps a command from running once per readiness poll (see
``ServiceReadinessService``).

Env is computed via ``EnvProvisionerService.compute``.  A ``ValueError`` (e.g. a
bad env-band template) does not crash ``service status``: it is caught by
``provision_scope_env``, surfaced as a diagnostic via the reporter, and degrades
that scope to **no injected env** — the scope's cells still run, reporting live
state without the ``WINTER_*`` / env-band values (same best-effort
resilience contract as describe errors in ``ServiceDescribeService.build``).
Because the env bands are workspace-global, a malformed template degrades every
scope; each emits its own diagnostic.

Per-cell invocation
-------------------
Each cell is invoked as:

    <entrypoint> status <scope>/*

(or ``<scope>/<svc>`` when the user supplied a service filter).  The explicit scope
pattern lets the provider report configured-but-stopped services via its existing
pattern expansion logic — no provider code change is needed.  Cells run in a bounded
worker pool (``concurrent.futures.ThreadPoolExecutor``).  Results are merged in
**enumeration order** (not completion order) to preserve deterministic output.

WINTER_SERVICE_MANIFEST
-----------------------
Status cells do NOT inject ``WINTER_SERVICE_MANIFEST``.  The manifest var is an
``up``-time contract: providers read it during startup to merge extension-declared
services into their live configuration.  Status does not start services; it only
queries what the provider already knows.  Injecting the manifest on status would
not change what the provider reports (it already ran ``up`` with the manifest),
and would add unnecessary subprocess overhead from ``ServiceManifestCollectorService``.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.provider_invocation import (
    IEnvProvisioner,
    build_provider_env,
    provision_scope_env,
)
from winter_cli.modules.service.scope import WORKSPACE_SCOPE
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_reporter import IServiceReporter
from winter_cli.modules.service.status_models import StatusDocument
from winter_cli.modules.service.status_parser import StatusDocumentParser, StatusParseError
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry

# Maximum number of provider-cell subprocesses that may run concurrently.
_MAX_WORKERS = 8


@dataclasses.dataclass(frozen=True)
class _StatusCell:
    """One (provider, scope) cell in the status call-matrix.

    ``scope`` is the env name (e.g. ``"alpha"``) or the literal string
    ``"workspace"``.  ``cell_pattern`` is the argv token forwarded to the provider
    (e.g. ``"alpha/*"`` or ``"workspace/*"``), optionally intersected with a
    user-supplied service filter.
    """

    provider: ResolvedCapability
    scope: str
    cell_pattern: str


def _env_seg_matches_scope(scope: str, env_seg: str) -> bool:
    """Match a pattern's env segment against a scope name.

    The ``workspace`` scope is reserved and distinct from the ``*`` (any-feature-env)
    wildcard: when either side names ``workspace`` the match is exact, so a bare
    ``*`` or other glob never sweeps in the workspace scope and a ``workspace``
    query never selects a feature-env scope. Otherwise the segment is matched via
    ``fnmatch`` so glob env names (``"al*"``, ``"*"``) select every configured env
    whose name matches, mirroring the segment-aware matching ``service_matches_pattern``
    already applies to describe identifiers.
    """
    if scope == WORKSPACE_SCOPE or env_seg == WORKSPACE_SCOPE:
        return scope == env_seg
    return fnmatch.fnmatchcase(scope, env_seg)


def _scope_matches_patterns(scope: str, patterns: tuple[str, ...]) -> bool:
    """Return True when *scope* should be included given *patterns*.

    With no patterns the scope is always included (full matrix).  A bare env
    pattern (no ``/``) matches the scope by name, honouring glob metacharacters
    (``"al*"`` matches every configured env starting with ``"al"``, ``"*"``
    matches every configured env). A scope-qualified pattern (``"gamma/web"``)
    matches when its env segment matches the scope name. ``"workspace"`` as a
    bare pattern matches the workspace scope; ``"workspace/<svc>"`` matches the
    workspace scope too — and, per ``_env_seg_matches_scope``, a glob pattern
    never sweeps in the workspace scope.
    """
    if not patterns:
        return True
    for pat in patterns:
        env_seg = pat.split("/", 1)[0] if "/" in pat else pat
        if _env_seg_matches_scope(scope, env_seg):
            return True
    return False


def cell_service_patterns(scope: str, patterns: tuple[str, ...]) -> list[str] | None:
    """Return the ordered, deduplicated service-segment filters that apply to *scope*.

    Returns ``None`` when *scope* carries no real per-service filter — either no
    patterns were supplied at all, or a bare/glob pattern matched the scope by name
    (``"gamma"``, ``"al*"``), which expands to every service in that scope. A
    non-``None`` list holds one entry per distinct service-segment pattern the user
    supplied that is scoped to *scope* (e.g. ``["db", "api"]`` for
    ``"alpha/db" "alpha/api"``).

    Exposed for ``ServiceDispatchService`` (winter#139 MUST-FIX): up/down cannot
    rely on the same whole-scope backstop ``status`` uses (there is no post-dispatch
    filter for up/down), so the dispatch path uses this to detect a genuine 2+
    service-filter case and emit one fan-out cell per service pattern instead of
    degrading to the whole scope.
    """
    if not patterns:
        return None

    svc_patterns: list[str] = []
    for pat in patterns:
        if "/" in pat:
            env_seg, svc_seg = pat.split("/", 1)
            if _env_seg_matches_scope(scope, env_seg) and svc_seg not in svc_patterns:
                svc_patterns.append(svc_seg)
        else:
            # Bare pattern matches by scope name (glob-aware) — expand to all services.
            if _env_seg_matches_scope(scope, pat):
                return None

    return svc_patterns or None


def _cell_argv_pattern(scope: str, patterns: tuple[str, ...]) -> str:
    """Build the scope-qualified pattern forwarded to the provider for this cell.

    When the user supplied a scope-qualified pattern (``"gamma/web"``) we pass
    ``"gamma/web"`` verbatim.  When no patterns, or patterns only name the scope
    (``"gamma"``, or a glob like ``"g*"``), we pass ``"<scope>/*"`` to get all
    services for that scope.
    """
    svc_patterns = cell_service_patterns(scope, patterns)
    if svc_patterns is None:
        return f"{scope}/*"
    if len(svc_patterns) == 1:
        return f"{scope}/{svc_patterns[0]}"
    # Multiple service patterns for this scope: forward <scope>/* so the
    # provider returns all services for this scope; the post-merge
    # filter_status backstop then narrows to only the matching ones. (up/down
    # has no such backstop — ServiceDispatchService detects this same
    # multi-filter case via cell_service_patterns and expands to per-service
    # cells instead of using this whole-scope pattern; see winter#139.)
    return f"{scope}/*"


def _provider_env_svc_matches_patterns(env_svcs: list[str], env_name: str, patterns: tuple[str, ...]) -> bool:
    """Return True when at least one of the provider's env service names matches a pattern.

    *env_svcs* are scope-qualified names like ``"*/db"``; the scope prefix ``*/``
    is stripped to get the plain service name, then matched against the service
    segment of each scope-qualified pattern whose env segment equals *env_name*.
    """
    plain_names = [s[2:] for s in env_svcs if s.startswith("*/")]
    for pat in patterns:
        if "/" in pat:
            env_seg, svc_seg = pat.split("/", 1)
            if _env_seg_matches_scope(env_name, env_seg) and any(fnmatch.fnmatchcase(n, svc_seg) for n in plain_names):
                return True
    return False


def _provider_ws_svc_matches_patterns(ws_svcs: list[str], patterns: tuple[str, ...]) -> bool:
    """Return True when at least one of the provider's workspace service names matches a pattern.

    *ws_svcs* are scope-qualified names like ``"workspace/rabbitmq"``; the prefix
    is stripped and matched against patterns whose env segment is ``"workspace"``.
    """
    plain_names = [s[len("workspace/") :] for s in ws_svcs if s.startswith("workspace/")]
    for pat in patterns:
        if "/" in pat:
            env_seg, svc_seg = pat.split("/", 1)
            if env_seg == "workspace" and any(fnmatch.fnmatchcase(n, svc_seg) for n in plain_names):
                return True
    return False


class ServiceStatusMatrixService:
    """Builds the status call-matrix, sources env, drives cells in parallel, merges.

    This is the Phase 2 implementation of the registry-driven enumeration
    described in winter#109.  It is used by ``ServiceStatusService.report`` and
    ``ServiceStatusService.collect`` as the multi-provider path.
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        describe_service: ServiceDescribeService,
        env_provisioner: IEnvProvisioner,
        status_parser: StatusDocumentParser,
        env_index_registry: IEnvIndexRegistry,
        workspace_root: Path,
        service_prefix: str,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._describe_service = describe_service
        self._env_provisioner = env_provisioner
        self._status_parser = status_parser
        self._env_index_registry = env_index_registry
        self._workspace_root = workspace_root
        self._service_prefix = service_prefix

    def build_matrix(
        self,
        providers: list[ResolvedCapability],
        patterns: tuple[str, ...],
        on_describe_error: Callable[[str, str], None] | None = None,
    ) -> list[_StatusCell]:
        """Build and return the ordered list of ``_StatusCell`` for *providers*.

        Single-provider short-circuit: when ``len(providers) == 1`` the describe
        call is skipped and every configured env + the workspace scope produce cells
        for the sole provider (mirroring ``ServiceProviderIndex.build`` D1).

        Multi-provider: ``describe`` is called on each provider; ownership
        determines which providers appear in env vs workspace cells.

        *patterns* narrows the matrix: scope-qualified patterns filter both the
        scope axis and (for multi-provider) the provider axis.  Bare env patterns
        filter only the scope axis.  No patterns → full matrix.
        """
        configured_envs: dict[str, int] = self._env_index_registry.all_assignments()

        if len(providers) == 1:
            return self._sole_provider_matrix(providers[0], configured_envs, patterns)

        return self._multi_provider_matrix(providers, configured_envs, patterns, on_describe_error)

    def known_envs(self) -> frozenset[str]:
        """Return the configured env names from the env-index registry.

        Exposed for restart's pre-dispatch pattern validation
        (``ServiceDispatchService._dispatch_restart``, winter#149) — the same
        registry-driven env catalog the up/down and status matrices already
        build the scope axis from.
        """
        return frozenset(self._env_index_registry.all_assignments())

    def run_matrix(
        self,
        cells: list[_StatusCell],
        reporter: IServiceReporter | None,
    ) -> tuple[list[StatusDocument], int]:
        """Invoke each cell in parallel, collect results in cell order.

        Returns ``(docs_in_order, first_nonzero_exit_code)``.  The exit code is
        the first non-zero exit seen in enumeration order (not the numeric
        maximum), matching the behaviour of the previous single-call path.
        A cell that returns a non-parseable status document is silently skipped
        (``collect`` contract) or surfaced via the reporter (``report`` contract).
        ``KeyboardInterrupt`` propagates as exit code 130 immediately.

        Env is provisioned once per scope and reused across providers.
        """
        # Pre-compute env once per unique scope.  A malformed env-band template
        # makes compute() raise ValueError; provision_scope_env catches it, emits a
        # diagnostic via the reporter, and degrades that scope to no injection
        # rather than letting a raw traceback escape and crash `service status`.
        unique_scopes = list(dict.fromkeys(cell.scope for cell in cells))
        provisioned: dict[str, dict[str, str]] = {}
        for scope in unique_scopes:
            # resolve_commands=False: health probing is provider-side and never
            # needs a command entry's real value. ServiceReadinessService calls
            # run_matrix on every poll (up to DEFAULT_WAIT_TIMEOUT_S /
            # DEFAULT_POLL_INTERVAL_S times per `--wait`), so gating status to
            # False is what keeps a command entry running once per scope (via
            # `up`) rather than once per poll.
            provisioned[scope] = provision_scope_env(self._env_provisioner, scope, reporter, resolve_commands=False)

        if not cells:
            return [], 0

        # Run cells in parallel, preserving order in results.
        active_cells = cells
        results: list[tuple[StatusDocument | None, int]] = [None] * len(active_cells)  # type: ignore[list-item]

        worst_exit = 0
        interrupted = False

        def _run_cell(idx: int, cell: _StatusCell) -> None:
            doc, code = self._invoke_cell(cell, provisioned.get(cell.scope, {}), reporter)
            results[idx] = (doc, code)

        try:
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                futures = [pool.submit(_run_cell, i, cell) for i, cell in enumerate(active_cells)]
                done, _ = wait(futures, return_when=FIRST_EXCEPTION)
                # Propagate KeyboardInterrupt if any future raised it.
                for f in done:
                    exc = f.exception()
                    if isinstance(exc, KeyboardInterrupt):
                        interrupted = True
                        break
        except KeyboardInterrupt:
            interrupted = True

        if interrupted:
            return [], 130

        docs: list[StatusDocument] = []
        for entry in results:
            if entry is None:
                continue
            doc, code = entry
            if code == 130:
                return [], 130
            if code != 0 and worst_exit == 0:
                worst_exit = code
            if doc is not None:
                docs.append(doc)

        return docs, worst_exit

    # ── private helpers ───────────────────────────────────────────────────────

    def _sole_provider_matrix(
        self,
        provider: ResolvedCapability,
        configured_envs: dict[str, int],
        patterns: tuple[str, ...],
    ) -> list[_StatusCell]:
        """Build matrix for the single-provider short-circuit.

        All configured envs get an ENV cell.  A WORKSPACE cell is always added
        (the provider may own workspace services; an empty response is valid).
        Both are filtered by *patterns*.
        """
        cells: list[_StatusCell] = []
        for env_name in sorted(configured_envs):
            if _scope_matches_patterns(env_name, patterns):
                cells.append(
                    _StatusCell(
                        provider=provider,
                        scope=env_name,
                        cell_pattern=_cell_argv_pattern(env_name, patterns),
                    )
                )
        if _scope_matches_patterns(WORKSPACE_SCOPE, patterns):
            cells.append(
                _StatusCell(
                    provider=provider,
                    scope=WORKSPACE_SCOPE,
                    cell_pattern=_cell_argv_pattern(WORKSPACE_SCOPE, patterns),
                )
            )
        return cells

    def _multi_provider_matrix(
        self,
        providers: list[ResolvedCapability],
        configured_envs: dict[str, int],
        patterns: tuple[str, ...],
        on_describe_error: Callable[[str, str], None] | None,
    ) -> list[_StatusCell]:
        """Build matrix for the multi-provider case.

        Delegates to ``ServiceDescribeService.build`` (the canonical describe→ownership
        path) to obtain a ``ServiceProviderIndex``.  The index provides
        ``names_owned_by(provider)`` to split a provider's services into env-scoped
        (``*/…``) vs workspace-scoped (``workspace/…``) names, and
        ``is_sole_provider`` for the single-provider short-circuit (though that branch
        is already handled by ``build_matrix`` before this method is called).

        Cells are ordered: all env cells (sorted by env name, then by provider order)
        followed by workspace cells (by provider order).
        """
        index = self._describe_service.build(providers, on_describe_error=on_describe_error)

        # Build per-provider taxonomy from the ownership index.
        # Service names are scope-qualified: "*/db", "workspace/rabbitmq".
        provider_taxonomy: dict[ResolvedCapability, tuple[list[str], list[str]]] = {}
        for provider in providers:
            owned = index.names_owned_by(provider)
            env_svcs = [s for s in owned if s.startswith("*/")]
            ws_svcs = [s for s in owned if s.startswith("workspace/")]
            provider_taxonomy[provider] = (env_svcs, ws_svcs)

        cells: list[_StatusCell] = []

        # Determine whether patterns restrict the provider axis.
        # When patterns are scope-qualified (e.g. "gamma/db"), a provider only
        # gets a cell if one of its described service names matches the pattern.
        # A bare env pattern (e.g. "gamma") makes no service-level restriction.
        has_svc_filter = bool(patterns) and all("/" in p for p in patterns)

        # ENV cells: for each env x each provider that owns env services.
        for env_name in sorted(configured_envs):
            if not _scope_matches_patterns(env_name, patterns):
                continue
            for provider in providers:
                taxonomy = provider_taxonomy.get(provider)
                if taxonomy is None:
                    continue
                env_svcs, _ = taxonomy
                if not env_svcs:
                    continue
                if has_svc_filter and not _provider_env_svc_matches_patterns(env_svcs, env_name, patterns):
                    continue
                cells.append(
                    _StatusCell(
                        provider=provider,
                        scope=env_name,
                        cell_pattern=_cell_argv_pattern(env_name, patterns),
                    )
                )

        # WORKSPACE cells: for each provider that owns workspace services.
        if _scope_matches_patterns(WORKSPACE_SCOPE, patterns):
            for provider in providers:
                taxonomy = provider_taxonomy.get(provider)
                if taxonomy is None:
                    continue
                _, ws_svcs = taxonomy
                if not ws_svcs:
                    continue
                if has_svc_filter and not _provider_ws_svc_matches_patterns(ws_svcs, patterns):
                    continue
                cells.append(
                    _StatusCell(
                        provider=provider,
                        scope=WORKSPACE_SCOPE,
                        cell_pattern=_cell_argv_pattern(WORKSPACE_SCOPE, patterns),
                    )
                )

        return cells

    def _invoke_cell(
        self,
        cell: _StatusCell,
        provisioned_env: dict[str, str],
        reporter: IServiceReporter | None,
    ) -> tuple[StatusDocument | None, int]:
        """Run one cell's status subprocess and return ``(doc_or_none, exit_code)``.

        Exit code 130 signals KeyboardInterrupt to the caller.  A parse failure
        when *reporter* is ``None`` (``collect`` path) silently returns
        ``(None, 0)`` so it does not block the poll loop.  When *reporter* is
        supplied (``report`` path) the error is surfaced via ``status_parse_error``.
        """
        # Build env: base provider vars → provisioned env (WINTER_ENV / INDEX /
        # WORKSPACE_PORT_BASE + env-band vars for workspace scope; additionally
        # WINTER_PORT_BASE for feature-env scopes).  The provisioner is the single
        # source of truth; there is no file to source.  For the workspace scope,
        # WINTER_PORT_BASE is deliberately NOT injected — the workspace band is
        # WINTER_WORKSPACE_PORT_BASE only, so the name carries one meaning everywhere.
        base_env = build_provider_env(cell.provider, self._workspace_root, self._service_prefix)
        merged_env = {**base_env, **provisioned_env}

        cmd = [str(cell.provider.entrypoint), "status", cell.cell_pattern]

        lines: list[str] = []
        exit_code = 0
        try:
            with self._subprocess_runner.popen(
                cmd, cwd=self._workspace_root, env=merged_env, merge_stderr=False
            ) as proc:
                try:
                    for line in proc.stdout_lines:
                        lines.append(line)
                except KeyboardInterrupt:
                    return None, 130
                exit_code = proc.wait()
        except KeyboardInterrupt:
            return None, 130

        raw = "\n".join(lines)
        try:
            doc: StatusDocument = self._status_parser.parse(raw)
        except StatusParseError as exc:
            if reporter is not None:
                reporter.status_parse_error(
                    str(cell.provider.entrypoint),
                    cell.provider.prefix,
                    str(exc),
                )
            return None, exit_code or 1

        return doc, exit_code
