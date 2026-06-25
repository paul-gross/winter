"""Captures and renders the orchestrator's structured status document.

The orchestrator is invoked as ``<entrypoint> status <scope>/*`` (or
``<scope>/<svc>`` for a service filter) with stdout piped.  Winter parses
the captured stdout as a ``StatusDocument``, applies the backstop filter,
then either re-serialises to canonical JSON (``--json``) or renders a human
table.  The orchestrator argv is byte-identical whether or not ``--json`` is
set — ``--json`` is never sent to the orchestrator.

Multi-provider call-matrix (Phase 2, winter#109)
-------------------------------------------------
The status action builds a two-dimensional call-matrix: rows are scope
instances (configured env names + the workspace scope), columns are owning
providers.  Core enumerates the matrix via ``ServiceStatusMatrixService``,
sources the correct ``.winter.env`` file per scope, injects
``WINTER_ENV``/``WINTER_ENV_INDEX``/``WINTER_PORT_BASE`` and the sourced
vars into each provider subprocess, runs cells in parallel, and merges the
per-cell documents into a single ``StatusDocument`` before filtering and
rendering.

For scope-qualified patterns (containing ``/``), the matrix is narrowed to
matching ``(provider, scope)`` cells — both the provider axis (ownership,
derived from ``describe``) and the scope axis (only matching envs/scopes
produce cells).  Bare patterns (``gamma``) narrow only the scope axis; all
providers that own services for that scope are included.  Bare ``status``
(no patterns) → full matrix.

Single-provider (D1 short-circuit)
-----------------------------------
When exactly one provider is bound, the call-matrix also applies: one cell per
configured env + one workspace cell, each with the injected env trio.  The
provider is invoked once per cell (not once for all scopes) so stopped envs are
enumerated via the explicit per-cell ``<env>/*`` pattern.

``collect`` (readiness gate)
-----------------------------
``collect`` (called by ``up --wait`` on every poll tick) is scoped to a single
env by construction — the pattern from ``ServiceReadinessService.wait`` is
always a bare env name.  The matrix degenerates to a single cell per provider
for that env, keeping the poll cheap.  ``collect`` silently skips parse errors
(unchanged).

Resolves providers and delegates to the injected ``ServiceStatusMatrixService``;
merges, filters, and renders results.
"""

from __future__ import annotations

from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_reporter import IServiceReporter
from winter_cli.modules.service.service_status_matrix_service import ServiceStatusMatrixService
from winter_cli.modules.service.status_filter import filter_status
from winter_cli.modules.service.status_merge import merge_status_documents
from winter_cli.modules.service.status_models import StatusDocument, StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser


class ServiceStatusService:
    """Resolves providers, delegates to the matrix service, merges/filters/renders results.

    The matrix service (``ServiceStatusMatrixService``) owns env enumeration,
    env-file sourcing, and parallel cell invocation.  This service owns the
    orchestrator resolution layer (``resolve_all``), the describe-error callback
    wired to the reporter, the ``no_service_matched`` early-exit, and the final
    merge/filter/render pass.

    Returns the first non-zero exit code across all cells, or 130 on KeyboardInterrupt.
    ``status_parser`` is injected to parse and serialise the orchestrator's JSON output.
    """

    def __init__(
        self,
        orchestrator_resolver: ServiceOrchestratorResolver,
        status_parser: StatusDocumentParser,
        matrix_service: ServiceStatusMatrixService,
    ) -> None:
        self._orchestrator_resolver = orchestrator_resolver
        self._status_parser = status_parser
        self._matrix_service = matrix_service

    def report(self, options: StatusOptions, reporter: IServiceReporter) -> int:
        """Run the status call-matrix and render the merged result."""
        providers = self._orchestrator_resolver.resolve_all()

        def _on_describe_error(name: str, detail: str) -> None:
            reporter.describe_parse_error(name, detail)

        cells = self._matrix_service.build_matrix(providers, options.patterns, on_describe_error=_on_describe_error)

        if not cells:
            # No provider owns any service matching the patterns.
            if options.patterns:
                reporter.no_service_matched(", ".join(repr(p) for p in options.patterns))
            return 1

        docs, worst_exit = self._matrix_service.run_matrix(cells, reporter)
        if worst_exit == 130:
            return 130

        merged = merge_status_documents(docs)
        merged = filter_status(merged, options.patterns)

        reporter.status_document(merged, self._status_parser)
        return worst_exit

    def collect(self, patterns: tuple[str, ...]) -> StatusDocument | None:
        """Fan out ``status`` across providers and return a merged, filtered document.

        This is the non-rendering counterpart of ``report`` used by the readiness
        gate on ``up --wait`` to poll health.  Parse errors are silently skipped
        so a broken provider does not block the poll.  ``KeyboardInterrupt``
        propagates to the caller.

        When scoped to a single env (the common readiness-gate case), the matrix
        degenerates to one cell per provider for that env — cheap and poll-safe.
        """
        providers = self._orchestrator_resolver.resolve_all()

        cells = self._matrix_service.build_matrix(providers, patterns, on_describe_error=None)
        if not cells:
            return None

        docs, worst_exit = self._matrix_service.run_matrix(cells, reporter=None)
        if worst_exit == 130:
            raise KeyboardInterrupt

        if not docs:
            return None

        merged = merge_status_documents(docs)
        return filter_status(merged, patterns)
