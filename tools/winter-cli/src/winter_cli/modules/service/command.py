from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import click
from dependency_injector import providers

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.service.handler import ServiceParams
from winter_cli.modules.service.models import LogOptions, parse_since_until
from winter_cli.modules.service.service_readiness_service import DEFAULT_WAIT_TIMEOUT_S
from winter_cli.modules.service.status_models import StatusOptions


def _load_spec_action_summaries() -> dict[str, str]:
    """Return {action_name: summary} derived from the bundled service-v1 spec.

    Called once at module import.  Loading the bundled TOML is fast (no network,
    no workspace discovery) and acceptable on this cold help-render path.
    Falls back to an empty dict if the spec cannot be loaded for any reason so
    the command structure remains usable even in degraded environments.
    """
    try:
        from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader
        from winter_cli.modules.capability.spec_loader import SpecLoader

        loader = SpecLoader(config_file_reader=TomllibConfigFileReader())
        spec = loader.load("service", "v1")
        return {action.name: action.summary for action in spec.actions}
    except Exception:
        return {}


_SPEC_SUMMARIES: dict[str, str] = _load_spec_action_summaries()

# Per-action short_help strings sourced from the spec.  The fallback literal
# mirrors what the spec says; it is only reached if the bundled TOML is absent
# (which should never happen in a normal install).
_HELP_UP = _SPEC_SUMMARIES.get("up", "Start all services in the named feature environment.")
_HELP_DOWN = _SPEC_SUMMARIES.get("down", "Stop all services in the named feature environment.")
_HELP_STATUS = _SPEC_SUMMARIES.get(
    "status",
    "Report the running state of matched services (defaults to all envs/services).",
)
_HELP_RESTART = _SPEC_SUMMARIES.get(
    "restart",
    "Restart one or more matched services without bringing down unmatched ones.",
)
_HELP_LOGS = _SPEC_SUMMARIES.get(
    "logs",
    "Stream or emit the log backlog for matched services as NDJSON on stdout.",
)
_HELP_DESCRIBE = _SPEC_SUMMARIES.get(
    "describe",
    "Emit a JSON object listing the service names owned by this provider.",
)
_HELP_CATALOG = "Emit scope-qualified service names from all providers as JSON."


def _service_handler(ctx: click.Context):
    """Resolve a ServiceHandler, injecting any active orchestrator override first.

    When `--service-orchestrator` or `WINTER_SERVICE_ORCHESTRATOR` is set, this
    overrides the container's `service_orchestrator_override` provider for the
    duration of this call so the resolver uses the local path or name supplied
    at the CLI boundary rather than the config-registered extension.
    """
    cli_context = cli_ctx(ctx)
    container = cli_context.container
    override = cli_context.service_orchestrator_override
    if override is not None:
        print(f"using service orchestrator override: {override}", file=sys.stderr)
        container.service_orchestrator_override.override(providers.Object(override))
    try:
        return container.service_handler()
    finally:
        if override is not None:
            container.service_orchestrator_override.reset_override()


@click.group("service")
def service_group() -> None:
    """Control workspace services via the registered orchestrator extension.

    Each action invokes the entrypoint registered in .winter/config.toml.
    `up`/`down` pass a single `<env>` positional; `restart`/`logs` forward
    `<env>/<service>` PATTERNS as positional argv tokens. `status` captures the
    orchestrator's stdout as a structured JSON document and renders it — patterns
    are forwarded on argv but `--json` is a winter-side render toggle only.
    `logs` render options (-f/-n/--since/--until/-t) are appended to the orchestrator
    argv as CLI flags after the positional patterns.
    """


@service_group.command("up", short_help=_HELP_UP)
@click.argument("env")
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="After starting, poll status until no service is unhealthy (every service healthy or unknown).",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=DEFAULT_WAIT_TIMEOUT_S,
    metavar="SECONDS",
    show_default=True,
    help="Max seconds to wait for readiness; only meaningful with --wait.",
)
@click.pass_context
def up_cmd(ctx: click.Context, env: str, wait: bool, timeout_s: float) -> None:
    """Start services for ENV.

    With ``--wait``, after dispatching ``up`` winter polls the orchestrator's
    ``status`` action and blocks until no in-scope service reports
    ``health: unhealthy`` (services reporting ``unknown`` — no declared probe —
    do not block), or ``--timeout`` SECONDS elapses. On timeout the command
    exits non-zero and names the still-unhealthy services on stderr. Without
    ``--wait``, ``up`` returns as soon as the orchestrator has launched the
    services, exactly as before.
    """
    handler = _service_handler(ctx)
    handler.run(ServiceParams(action="up", env=env, wait=wait, timeout_s=timeout_s))


@service_group.command("down", short_help=_HELP_DOWN)
@click.argument("env")
@click.pass_context
def down_cmd(ctx: click.Context, env: str) -> None:
    """Stop services for ENV."""
    handler = _service_handler(ctx)
    handler.run(ServiceParams(action="down", env=env))


@service_group.command("status", short_help=_HELP_STATUS)
@click.argument("patterns", nargs=-1)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the structured status document as JSON.")
@click.pass_context
def status_cmd(ctx: click.Context, patterns: tuple[str, ...], as_json: bool) -> None:
    """Report status of services matching <env>/<service> PATTERNS.

    The orchestrator is invoked as ``<entrypoint> status <pattern...>`` and must
    emit a JSON status document on stdout.  Winter parses and renders the result:
    a human table by default, or the canonical JSON document under ``--json``.
    ``--json`` is a pure winter-side render toggle and is never sent to the
    orchestrator.

    PATTERNS are zero or more <env>/<service> segment-glob strings. Omit to
    report all services across all environments. Patterns are forwarded verbatim
    as positional argv to the orchestrator entrypoint; winter also applies a
    backstop filter on the parsed document.
    """
    handler = _service_handler(ctx)
    options = StatusOptions(patterns=patterns, as_json=as_json)
    handler.run_status(options)


@service_group.command("restart", short_help=_HELP_RESTART)
@click.argument("patterns", nargs=-1, required=True)
@click.pass_context
def restart_cmd(ctx: click.Context, patterns: tuple[str, ...]) -> None:
    """Restart every service matching <env>/<service> PATTERNS.

    PATTERNS are one or more <env>/<service> segment-glob strings (at least one
    required). Patterns are forwarded verbatim as positional argv to the
    orchestrator entrypoint; the orchestrator is responsible for expanding them
    against its declared service catalog.

    At least one pattern is required because action commands require an explicit
    target — there is no implicit "everything", mirroring `winter ws merge`
    requiring a source ref (unlike read-shaped commands such as `status` that
    default to all).
    """
    handler = _service_handler(ctx)
    handler.run(ServiceParams(action="restart", patterns=patterns))


def _validate_tail(ctx: click.Context, param: click.Parameter, value: str) -> int | str:
    """Validate --tail: must be a positive integer or the literal 'all'."""
    if value == "all":
        return "all"
    try:
        n = int(value)
    except ValueError:
        raise click.BadParameter("must be a positive integer or 'all'", ctx=ctx, param=param) from None
    if n <= 0:
        raise click.BadParameter("must be a positive integer or 'all'", ctx=ctx, param=param)
    return n


@service_group.command("logs", short_help=_HELP_LOGS)
@click.argument("patterns", nargs=-1, required=True)
@click.option("-f", "--follow", is_flag=True, default=False, help="Stream until interrupted.")
@click.option(
    "-n",
    "--tail",
    default="200",
    metavar="N",
    callback=_validate_tail,
    is_eager=False,
    help="Last N lines (positive integer or 'all'). Default: 200.",
)
@click.option("--since", default="", metavar="DUR|TS", help="Show logs since duration or RFC3339 timestamp.")
@click.option("--until", default="", metavar="DUR|TS", help="Show logs until duration or RFC3339 timestamp.")
@click.option("-t", "--timestamps", is_flag=True, default=False, help="Prefix each line with its timestamp.")
@click.pass_context
def logs_cmd(
    ctx: click.Context,
    patterns: tuple[str, ...],
    follow: bool,
    tail: int | str,
    since: str,
    until: str,
    timestamps: bool,
) -> None:
    """Stream logs for services matching PATTERNS.

    PATTERNS are one or more <env>/<service> segment-glob strings (at least one
    required — action commands require an explicit target, unlike `status` which
    defaults to all). Selection is forwarded verbatim as positional argv to the
    orchestrator entrypoint (`<entrypoint> logs <pattern...>`). Winter applies a
    segment-aware backstop filter on the NDJSON stream: each line's env/svc is
    matched against the patterns via matches_any_pattern; lines missing env or
    svc are dropped. Render options are appended to that argv as CLI flags.

    \b
    Duration format: <N>(s|m|h|d) — e.g. 90s, 5m, 2h, 3d.
    Timestamp format: RFC3339 — e.g. 2026-06-13T10:00:00Z.

    When --follow is set, winter relays lines live and does NOT re-apply tail
    (the orchestrator is expected to honour the --tail flag). Interrupted with
    Ctrl-C exits 130.
    """
    now = datetime.now(tz=UTC)

    since_rfc3339 = ""
    if since:
        try:
            since_rfc3339 = parse_since_until(since, now)
        except ValueError as exc:
            raise click.BadParameter(str(exc), ctx=ctx, param_hint="'--since'") from exc

    until_rfc3339 = ""
    if until:
        try:
            until_rfc3339 = parse_since_until(until, now)
        except ValueError as exc:
            raise click.BadParameter(str(exc), ctx=ctx, param_hint="'--until'") from exc

    options = LogOptions(
        patterns=patterns,
        follow=follow,
        tail=tail,
        since_rfc3339=since_rfc3339,
        until_rfc3339=until_rfc3339,
        timestamps=timestamps,
    )
    handler = _service_handler(ctx)
    handler.run_logs(options)


@service_group.command("catalog", short_help=_HELP_CATALOG, hidden=True)
@click.pass_context
def catalog_cmd(ctx: click.Context) -> None:
    """Emit the merged service catalog from all bound providers as JSON.

    Each provider is queried with the ``catalog`` action and returns its
    declared services as scope-qualified names.  Winter merges the results
    across all providers and emits a single JSON object::

        {"services": ["workspace/postgres", "*/api", "*/worker"]}

    ``workspace/<name>`` means the service runs in the shared workspace scope.
    ``*/<name>`` means the service runs per feature env (any env name matches).

    Used by ``winter lint`` to validate ``required_services`` references in
    provision manifests.
    """
    from winter_cli.modules.service.service_catalog_service import ServiceCatalogService

    cli_context = cli_ctx(ctx)
    container = cli_context.container
    override = cli_context.service_orchestrator_override
    if override is not None:
        print(f"using service orchestrator override: {override}", file=sys.stderr)
        container.service_orchestrator_override.override(providers.Object(override))
    try:
        resolver = container.service_orchestrator_resolver()
        all_providers = resolver.resolve_all()
    except Exception as exc:
        print(json.dumps({"services": [], "error": str(exc)}))
        if override is not None:
            container.service_orchestrator_override.reset_override()
        return
    finally:
        if override is not None:
            container.service_orchestrator_override.reset_override()

    subprocess_runner = container.subprocess_runner()
    workspace_config = container.workspace_config()
    catalog_svc = ServiceCatalogService(subprocess_runner, workspace_config.workspace_root)
    catalog = catalog_svc.build(all_providers)
    print(json.dumps({"services": catalog.all_qualified_names()}))


@service_group.command("describe", short_help=_HELP_DESCRIBE, hidden=True)
@click.pass_context
def describe_cmd(ctx: click.Context) -> None:
    """Emit a JSON object listing the service names owned by this provider.

    This is a provider-contract action used by winter internally when routing
    across multiple service orchestrator providers (via ``capabilities.service = [...]``
    in .winter/config.toml).  The provider entrypoint must emit
    ``{"services": ["name", ...]}`` on stdout.  Unknown or empty → ``{"services": []}``.

    Calling this subcommand directly dispatches to the single registered provider.
    When multiple providers are bound, each is queried in turn by winter during
    ``up``/``down``/``logs``/``restart`` to build the service ownership index.
    """
    handler = _service_handler(ctx)
    handler.run(ServiceParams(action="describe"))
