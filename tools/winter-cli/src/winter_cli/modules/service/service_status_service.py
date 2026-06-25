"""Captures and renders the orchestrator's structured status document.

The orchestrator is invoked as ``<entrypoint> status [pattern...]`` with stdout
piped.  Winter parses the captured stdout as a ``StatusDocument``, applies the
backstop filter, then either re-serialises to canonical JSON (``--json``) or
renders a human table.  The orchestrator argv is byte-identical whether or not
``--json`` is set — ``--json`` is never sent to the orchestrator.

With multiple providers, each provider's ``status`` output is parsed and merged
into a single ``StatusDocument`` before filtering and rendering.  A provider that
emits a non-conformant document surfaces a clear error naming that provider, and
the worst exit code across providers is adopted.

For scope-qualified patterns (containing ``/``), the describe ownership index is
consulted to dispatch only to the provider(s) that own matching services.  Bare
patterns (no ``/``) retain the existing full fan-out across all providers.

Returns the orchestrator's exit code, or 130 on KeyboardInterrupt.  When the
orchestrator's stdout cannot be parsed as a conformant status document a clear
actionable message is written to stderr and the exit code is the orchestrator's
own non-zero code (or 1 if the orchestrator exited 0).
"""

from __future__ import annotations

from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.provider_invocation import build_provider_env, service_matches_pattern
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_reporter import IServiceReporter
from winter_cli.modules.service.status_filter import filter_status
from winter_cli.modules.service.status_merge import merge_status_documents
from winter_cli.modules.service.status_models import StatusDocument, StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser, StatusParseError


class ServiceStatusService:
    """Captures and renders the orchestrator's structured status document.

    Invokes the orchestrator entrypoint as ``<entrypoint> status <pattern...>``
    with cwd at the workspace root.  Patterns are forwarded verbatim as
    positional argv tokens.  The three context vars ``WINTER_WORKSPACE_DIR``,
    ``WINTER_EXT_DIR``, and ``WINTER_EXT_PREFIX`` are exported; no status-specific
    env vars are added.  The orchestrator's stderr inherits the parent's fd so
    diagnostics reach the terminal without corrupting the JSON stream.

    With multiple providers (via ``capabilities.service = [...]`` or implicit-all),
    each provider's ``status`` output is independently parsed and the results are
    merged into a single ``StatusDocument`` before filtering and rendering.  A
    provider whose output cannot be parsed surfaces an actionable error naming that
    specific provider; the worst exit code across all providers is adopted.

    Returns the orchestrator's exit code, or 130 if interrupted by KeyboardInterrupt.
    ``status_parser`` is injected to parse and serialise the orchestrator's JSON output.
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        orchestrator_resolver: ServiceOrchestratorResolver,
        status_parser: StatusDocumentParser,
        workspace_root: Path,
        describe_service: ServiceDescribeService | None = None,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._orchestrator_resolver = orchestrator_resolver
        self._status_parser = status_parser
        self._workspace_root = workspace_root
        self._describe_service = describe_service

    def report(self, options: StatusOptions, reporter: IServiceReporter) -> int:
        """Run the orchestrator status entrypoint and render the result."""
        providers = self._orchestrator_resolver.resolve_all()

        # D1 short-circuit: single provider — existing behavior unchanged.
        if len(providers) == 1:
            return self._report_single(providers[0], options, reporter)

        # Multi-provider: scope-qualified patterns route to owning providers only;
        # bare patterns fan out to all providers.
        active_providers = self._route_providers(providers, options.patterns, reporter)
        if active_providers is None:
            # All patterns were scope-qualified and none matched any owned service.
            return 1

        docs: list[StatusDocument] = []
        worst_exit = 0

        for provider in active_providers:
            doc, exit_code = self._fetch_provider_status(provider, options, reporter)
            if exit_code == 130:
                return 130
            if exit_code != 0 and worst_exit == 0:
                worst_exit = exit_code
            if doc is not None:
                docs.append(doc)

        merged = merge_status_documents(docs)
        merged = filter_status(merged, options.patterns)

        reporter.status_document(merged, self._status_parser)
        return worst_exit

    def collect(self, patterns: tuple[str, ...]) -> StatusDocument | None:
        """Fan out the `status` action across providers and return a merged, filtered document.

        This is the non-rendering counterpart of ``report`` — it reuses the same
        per-provider invocation, parse, merge, and filter path but returns the
        ``StatusDocument`` to the caller instead of handing it to a reporter.
        It is used by the readiness gate on ``up --wait`` to poll health.

        A provider whose stdout cannot be parsed contributes no document (silently,
        since this runs in a poll loop). Returns ``None`` only when no provider
        produced a parseable document at all. ``KeyboardInterrupt`` propagates to
        the caller. Patterns scope the result exactly as for ``status`` (a bare
        ``<env>`` expands to ``<env>/*``).

        For scope-qualified patterns (containing ``/``), only the owning provider(s)
        are queried.  Bare patterns retain full fan-out.  An unowned scope-qualified
        pattern is treated as not-running (returns ``None`` or excludes it from the
        merged document), consistent with the required_services gate semantics.
        """
        providers = self._orchestrator_resolver.resolve_all()

        # Scope-qualified routing: when a describe_service is available and all
        # patterns are scope-qualified, restrict to owning providers only.
        active_providers = self._route_providers(providers, patterns, reporter=None)
        if active_providers is None:
            # All patterns scope-qualified but no provider owns any of them.
            return None

        docs: list[StatusDocument] = []
        for provider in active_providers:
            raw, _exit_code = self._capture_status(provider, patterns)
            try:
                docs.append(self._status_parser.parse(raw))
            except StatusParseError:
                continue

        if not docs:
            return None

        merged = merge_status_documents(docs)
        return filter_status(merged, patterns)

    def _route_providers(
        self,
        providers: list[ResolvedCapability],
        patterns: tuple[str, ...],
        reporter: IServiceReporter | None,
    ) -> list[ResolvedCapability] | None:
        """Return the subset of providers to invoke for the given patterns.

        When no describe_service is available, or when any pattern is bare (no
        ``/``), returns the full provider list unchanged (full fan-out).

        When all patterns are scope-qualified (contain ``/``) and a describe_service
        is available, builds the ownership index and returns only the providers that
        own at least one matching service.  If no provider owns any pattern, emits a
        single actionable ``no_service_matched`` diagnostic (when ``reporter`` is not
        None) and returns ``None``.

        Describe errors from individual providers are surfaced via the reporter's
        ``describe_parse_error`` (when supplied) and those providers contribute no
        services to the index — consistent with the resilient path in
        ``ServiceDescribeService.build``.
        """
        # No routing possible without a describe service.
        if self._describe_service is None:
            return providers

        # Bare patterns → full fan-out (unchanged behavior).
        if not patterns or any("/" not in p for p in patterns):
            return providers

        # All patterns are scope-qualified — route via the ownership index.
        def _on_error(provider_name: str, detail: str) -> None:
            if reporter is not None:
                reporter.describe_parse_error(provider_name, detail)

        index = self._describe_service.build(list(providers), on_describe_error=_on_error)

        # Collect owning providers for each pattern.
        owning: set[ResolvedCapability] = set()
        unmatched_patterns: list[str] = []
        for pat in patterns:
            matched = False
            for svc_name in index.known_service_names():
                if service_matches_pattern(svc_name, pat):
                    owner = index.owner_for(svc_name)
                    if owner is not None:
                        owning.add(owner)
                        matched = True
            if not matched:
                unmatched_patterns.append(pat)

        if not owning:
            # No provider owns any of the requested services.
            if reporter is not None:
                token_list = ", ".join(repr(p) for p in patterns)
                reporter.no_service_matched(token_list)
            return None

        # Return providers in the original order (preserves deterministic output).
        return [p for p in providers if p in owning]

    def _capture_status(self, provider: ResolvedCapability, patterns: tuple[str, ...]) -> tuple[str, int]:
        """Run ``<entrypoint> status [pattern...]`` and return ``(raw_stdout, exit_code)``.

        ``KeyboardInterrupt`` propagates to the caller rather than being mapped to a
        sentinel exit code — the polling caller owns interrupt handling.
        """
        cmd = [str(provider.entrypoint), "status", *patterns]
        env = build_provider_env(provider, self._workspace_root)

        lines: list[str] = []
        with self._subprocess_runner.popen(cmd, cwd=self._workspace_root, env=env, merge_stderr=False) as proc:
            for line in proc.stdout_lines:
                lines.append(line)
            exit_code = proc.wait()

        return "\n".join(lines), exit_code

    def _fetch_provider_status(
        self,
        provider: ResolvedCapability,
        options: StatusOptions,
        reporter: IServiceReporter,
    ) -> tuple[StatusDocument | None, int]:
        """Run one provider's status action and return (parsed doc or None, exit_code).

        On KeyboardInterrupt returns (None, 130).  On parse failure writes an
        actionable error to stderr naming the specific provider, sets a non-zero
        exit code, and returns (None, code).
        """
        cmd = [str(provider.entrypoint), "status", *options.patterns]

        merged_env = build_provider_env(provider, self._workspace_root)

        exit_code = 0
        lines: list[str] = []
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
            reporter.status_parse_error(
                str(provider.entrypoint),
                provider.prefix,
                str(exc),
            )
            return None, exit_code or 1

        return doc, exit_code

    def _report_single(self, provider: ResolvedCapability, options: StatusOptions, reporter: IServiceReporter) -> int:
        """Single-provider path — existing behavior unchanged."""
        cmd = [str(provider.entrypoint), "status", *options.patterns]

        merged = build_provider_env(provider, self._workspace_root)

        exit_code = 0
        lines: list[str] = []
        try:
            with self._subprocess_runner.popen(cmd, cwd=self._workspace_root, env=merged, merge_stderr=False) as proc:
                try:
                    for line in proc.stdout_lines:
                        lines.append(line)
                except KeyboardInterrupt:
                    return 130

                exit_code = proc.wait()
        except KeyboardInterrupt:
            return 130

        raw = "\n".join(lines)
        try:
            doc: StatusDocument = self._status_parser.parse(raw)
        except StatusParseError as exc:
            reporter.status_parse_error(
                str(provider.entrypoint),
                provider.prefix,
                str(exc),
            )
            return exit_code or 1

        doc = filter_status(doc, options.patterns)

        reporter.status_document(doc, self._status_parser)
        return exit_code
