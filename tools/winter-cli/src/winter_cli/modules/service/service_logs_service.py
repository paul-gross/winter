from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.log_stream_processor import LogStreamProcessor
from winter_cli.modules.service.models import LogOptions
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.provider_invocation import service_matches_pattern
from winter_cli.modules.service.service_provider_index import ServiceDescribeService


class ServiceLogsService:
    """Streams logs from the registered orchestrator(s) via the winter-defined contract.

    Single-provider (D1 short-circuit): invokes the sole orchestrator entrypoint as
    ``<entrypoint> logs <pattern...>`` with ``cwd`` at the workspace root. The
    ``<env>/<service>`` selection patterns are forwarded verbatim as positional argv
    tokens. Render parameters are conveyed via ``WINTER_LOG_*`` environment variables
    (``WINTER_LOG_FOLLOW``, ``WINTER_LOG_TAIL``, ``WINTER_LOG_SINCE``,
    ``WINTER_LOG_UNTIL``, ``WINTER_LOG_TIMESTAMPS``). Like every dispatch it also
    exports ``WINTER_WORKSPACE_DIR``, ``WINTER_EXT_DIR``, and ``WINTER_EXT_PREFIX``.
    The orchestrator's stdout is read as NDJSON; each line must carry an ``env`` field
    in addition to ``svc``/``msg``; winter applies a segment-aware backstop filter
    matching ``<env>/<svc>`` against the requested patterns, then applies time/tail
    filters and renders plain lines to stdout. The orchestrator's stderr inherits the
    parent's fd so diagnostics reach the terminal without corrupting the NDJSON stream.

    Multi-provider: builds the service ownership index via ``ServiceDescribeService``,
    determines which providers own the matched services, and routes each provider's
    ``logs`` action with only the patterns it owns. The output streams from each
    provider are merged through a shared ``LogStreamProcessor`` so the caller sees
    a single unified stream.

    Follow mode (D2): ``-f`` / ``follow=True`` is supported only when the matched
    services resolve to a **single** owning provider. When a follow request would
    span multiple owning providers, this method writes an actionable error to stderr
    and returns 1 without opening any stream.

    Returns the orchestrator's exit code (worst across providers), or 130 if
    interrupted by KeyboardInterrupt.
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        orchestrator_resolver: ServiceOrchestratorResolver,
        describe_service: ServiceDescribeService,
        click: Any,
        workspace_root: Path,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._orchestrator_resolver = orchestrator_resolver
        self._describe_service = describe_service
        self._click = click
        self._workspace_root = workspace_root

    def stream(self, options: LogOptions) -> int:
        """Run the orchestrator logs entrypoint and stream rendered output to stdout."""
        providers = self._orchestrator_resolver.resolve_all()

        # D1 short-circuit: single provider — forward verbatim, no describe call.
        if len(providers) == 1:
            return self._stream_single(providers[0], options, options.patterns)

        # Multi-provider: build the ownership index and route patterns to owners.
        index = self._describe_service.build(providers)

        # Determine which providers own the requested patterns.
        # For each pattern, find the owning provider for matching service names
        # and record the original pattern token (not the bare service name) so
        # the provider receives env-scoped tokens like "alpha/backend".
        owning_providers: list[ResolvedCapability] = []
        provider_patterns_map: dict[str, list[str]] = {p.extension_name: [] for p in providers}

        known_services = list(index.known_service_names())
        matched_patterns: set[str] = set()
        for pat in options.patterns:
            for svc_name in known_services:
                owner = index.owner_for(svc_name)
                if owner is None:
                    continue
                if service_matches_pattern(svc_name, pat):
                    matched_patterns.add(pat)
                    if owner not in owning_providers:
                        owning_providers.append(owner)
                    if pat not in provider_patterns_map[owner.extension_name]:
                        provider_patterns_map[owner.extension_name].append(pat)

        # Emit no-match diagnostic for patterns that resolved to no known service.
        if options.patterns:
            unmatched = [p for p in options.patterns if p not in matched_patterns]
            if unmatched:
                token_list = ", ".join(repr(p) for p in unmatched)
                self._click.echo(f"no service matched {token_list}", err=True)

        # If no patterns were requested, route all providers (empty selection = all).
        if not options.patterns:
            owning_providers = list(providers)
            for p in providers:
                provider_patterns_map[p.extension_name] = []

        # D2: -f with multiple owning providers is an error.
        if options.follow and len(owning_providers) > 1:
            provider_names = ", ".join(p.extension_name for p in owning_providers)
            self._click.echo(
                f"error: --follow (-f) cannot span multiple service providers "
                f"({provider_names}). "
                f"Narrow your selection to services owned by a single provider, "
                f"or omit -f to merge non-follow logs across providers.",
                err=True,
            )
            return 1

        # Fan out: drain each owning provider through the shared processor.
        processor = LogStreamProcessor(options)
        exit_code = 0

        for provider in owning_providers:
            owned_patterns = provider_patterns_map[provider.extension_name]
            # Use the owned service names as patterns; empty list means all.
            patterns_for_provider = tuple(owned_patterns) if owned_patterns else options.patterns
            code = self._stream_single(provider, options, patterns_for_provider, processor=processor)
            if code == 130:
                return 130
            if code != 0 and exit_code == 0:
                exit_code = code

        # Flush the shared processor's tail ring buffer (non-follow, accumulated).
        # Always finalize in the multi-provider code path — even when only one
        # provider matched, the shared processor holds results in its ring buffer
        # that must be flushed (since own_processor=False in _stream_single).
        if owning_providers:
            for rendered in processor.finalize():
                self._click.echo(rendered)
            self._emit_warnings(processor)

        return exit_code

    def _stream_single(
        self,
        provider: ResolvedCapability,
        options: LogOptions,
        patterns: tuple[str, ...],
        *,
        processor: LogStreamProcessor | None = None,
    ) -> int:
        """Stream logs from a single provider, optionally sharing a processor.

        When ``processor`` is None (single-provider path), creates a fresh one
        and handles finalization and warnings itself.  When supplied (multi-provider
        fan-out), accumulates into the shared processor without finalizing (the
        caller handles that).
        """
        cmd = [str(provider.entrypoint), "logs", *patterns]

        extra_env = dict(os.environ)
        extra_env["WINTER_LOG_FOLLOW"] = "1" if options.follow else "0"
        extra_env["WINTER_LOG_TAIL"] = str(options.tail)
        extra_env["WINTER_LOG_SINCE"] = options.since_rfc3339
        extra_env["WINTER_LOG_UNTIL"] = options.until_rfc3339
        extra_env["WINTER_LOG_TIMESTAMPS"] = "1" if options.timestamps else "0"
        extra_env["WINTER_WORKSPACE_DIR"] = str(self._workspace_root)
        extra_env["WINTER_EXT_DIR"] = str(provider.ext_dir)
        extra_env["WINTER_EXT_PREFIX"] = provider.prefix

        own_processor = processor is None
        if own_processor:
            processor = LogStreamProcessor(options)

        exit_code = 0
        try:
            with self._subprocess_runner.popen(
                cmd, cwd=self._workspace_root, env=extra_env, merge_stderr=False
            ) as proc:
                try:
                    for rendered in processor.process_lines(proc.stdout_lines):
                        self._click.echo(rendered)
                except KeyboardInterrupt:
                    return 130

                if own_processor:
                    for rendered in processor.finalize():
                        self._click.echo(rendered)

                exit_code = proc.wait()
        except KeyboardInterrupt:
            return 130

        if own_processor:
            self._emit_warnings(processor)

        return exit_code

    def _emit_warnings(self, processor: LogStreamProcessor) -> None:
        """Emit accumulated processor warnings to stderr (once each)."""
        if processor.timestamps_warning:
            self._click.echo(
                "warning: the orchestrator supplies no per-line timestamps — "
                "timestamp prefixes omitted for affected lines",
                err=True,
            )
        if processor.time_filter_warning:
            self._click.echo(
                "warning: the orchestrator supplies no per-line timestamps — "
                "--since/--until filter is partial (applied only to lines with a ts field)",
                err=True,
            )
