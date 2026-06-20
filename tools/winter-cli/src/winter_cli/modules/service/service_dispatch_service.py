from __future__ import annotations

from pathlib import Path
from typing import Any

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.provider_invocation import build_provider_env, service_matches_pattern
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService


class ServiceDispatchService:
    """Dispatches up/down/restart to the registered service orchestrator(s).

    For ``up`` and ``down``, uses ``ServiceFanOutService`` to fan out across the
    ordered provider list with no readiness gate or ordering semantics.

    For ``restart`` with multiple providers, builds the service-to-provider ownership
    index via ``ServiceDescribeService``, groups matched services by owning provider,
    and dispatches each provider only the services it owns.  With a single provider,
    ``restart`` behaves exactly as before (no ``describe`` call, patterns forwarded
    verbatim).

    For other actions (``describe``, etc.), dispatches to the single resolved provider
    via the orchestrator resolver, as before.

    The orchestrator is invoked as `<entrypoint> <action> [positional...]` (argv),
    with `cwd` at the workspace root. Every dispatch exports `WINTER_WORKSPACE_DIR`,
    `WINTER_EXT_DIR`, and `WINTER_EXT_PREFIX` (matching the doctor/lint/hook
    dispatches).

    For up/down the single positional is `<env>`. For restart the positionals are
    the verbatim `<env>/<service>` selection PATTERNS forwarded unchanged on argv.
    No per-action selection env vars are set here (status is handled separately by
    ServiceStatusService; logs is handled separately by ServiceLogsService).

    The entrypoint's exit code is returned unmodified; stdout/stderr are
    inherited from the parent process (no capture).
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        orchestrator_resolver: ServiceOrchestratorResolver,
        fan_out_service: ServiceFanOutService,
        describe_service: ServiceDescribeService,
        workspace_root: Path,
        click: Any = None,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._orchestrator_resolver = orchestrator_resolver
        self._fan_out_service = fan_out_service
        self._describe_service = describe_service
        self._workspace_root = workspace_root
        self._click = click

    def dispatch(self, action: str, positionals: list[str]) -> int:
        """Run the orchestrator's entrypoint and return its exit code unmodified."""
        if action == "up":
            env = positionals[0] if positionals else ""
            providers = self._orchestrator_resolver.resolve_all()
            return self._fan_out_service.up(env, providers)

        if action == "down":
            env = positionals[0] if positionals else ""
            providers = self._orchestrator_resolver.resolve_all()
            return self._fan_out_service.down(env, providers)

        if action == "restart":
            return self._dispatch_restart(positionals)

        # For all other actions (describe, …), fall through to the
        # single-provider path via the orchestrator resolver.
        resolved = self._orchestrator_resolver.resolve()
        cmd = [str(resolved.entrypoint), action, *positionals]
        merged = build_provider_env(resolved, self._workspace_root)
        return self._subprocess_runner.call(cmd, cwd=self._workspace_root, env=merged)

    def _dispatch_restart(self, patterns: list[str]) -> int:
        """Route restart to the owning provider(s) based on the service ownership index.

        D1 short-circuit: with a single provider, no describe call is made; the
        provider receives all patterns verbatim.

        With multiple providers, the index is built, each user pattern is matched
        against the known service names, and each provider receives only the
        original pattern tokens that match services it owns (in the user-supplied
        order, deduplicated per provider).  A pattern matching no service in any
        provider emits a diagnostic to stderr.  A provider with no matched
        patterns is not invoked.
        """
        providers = self._orchestrator_resolver.resolve_all()

        # D1: single-provider short-circuit — no describe, forward verbatim.
        if len(providers) == 1:
            provider = providers[0]
            return self._call_provider(provider, "restart", patterns)

        # Multi-provider: build the ownership index.
        index = self._describe_service.build(providers)

        # Collect all known service names from the index.
        known_services = list(index.known_service_names())

        # For each provider, collect the original pattern tokens that match its
        # owned services.  Each pattern is forwarded at most once per provider
        # (deduplicated while preserving first-match order).
        provider_patterns: dict[str, list[str]] = {p.extension_name: [] for p in providers}

        matched_patterns: set[str] = set()
        for pat in patterns:
            for svc_name in known_services:
                owner = index.owner_for(svc_name)
                if owner is None:
                    continue
                if service_matches_pattern(svc_name, pat):
                    matched_patterns.add(pat)
                    owned = provider_patterns[owner.extension_name]
                    if pat not in owned:
                        owned.append(pat)

        # Emit no-match diagnostic for patterns that resolved to no known service.
        unmatched = [p for p in patterns if p not in matched_patterns]
        if unmatched and self._click is not None:
            token_list = ", ".join(repr(p) for p in unmatched)
            self._click.echo(
                f"no service matched {token_list}",
                err=True,
            )

        # Dispatch each provider that owns matched patterns.
        exit_code = 0
        for provider in providers:
            owned = provider_patterns.get(provider.extension_name, [])
            if not owned:
                continue
            code = self._call_provider(provider, "restart", owned)
            if code != 0 and exit_code == 0:
                exit_code = code

        return exit_code

    def _call_provider(self, provider: ResolvedCapability, action: str, positionals: list[str]) -> int:
        cmd = [str(provider.entrypoint), action, *positionals]
        merged = build_provider_env(provider, self._workspace_root)
        return self._subprocess_runner.call(cmd, cwd=self._workspace_root, env=merged)
