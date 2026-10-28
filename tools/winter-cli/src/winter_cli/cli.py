from __future__ import annotations

import importlib
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from winter_cli.container import Container


def _bytecode_cache_prefix() -> str:
    """Per-user directory that winter's redirected bytecode cache mirrors into.

    Honors `XDG_CACHE_HOME`, falling back to `~/.cache`.
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return str(base / "winter" / "pycache")


# Redirect the bytecode cache out of every source tree, replacing the old
# process-wide `sys.dont_write_bytecode = True`. That flag stopped winter from
# scribbling `__pycache__/` into plugin extension source trees (plugins are
# exec'd from their own repos via importlib), but as a side effect the core
# `winter_cli` package never got a `.pyc` cache either, so every run recompiled
# from source. Pointing `sys.pycache_prefix` at a per-user cache dir mirrors
# compiled modules under that prefix instead of next to their source:
# `winter_cli` gets a warm cache across runs while plugin (and all other)
# source trees stay clean. Set before importing any winter_cli submodule so
# their first compile already lands under the prefix. A pre-set prefix (caller
# override) wins.
if sys.pycache_prefix is None:
    sys.pycache_prefix = _bytecode_cache_prefix()

import click

from winter_cli.cli_context import CliContext, LazyContainer
from winter_cli.core.config_file import ConfigError
from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
from winter_cli.core.ssh_keepalives import ensure_ssh_keepalives
from winter_cli.core.tracing import (
    ICommandTracer,
    TracingSettings,
    non_blank,
    parse_launch_time_ns,
    parse_sdk_disabled,
)


class LazySubcommand(NamedTuple):
    """One top-level command: where it lives, and the text its listed short help derives from.

    `help` is the command's first help paragraph. `--help` renders the listing from it with click's
    own truncation, so the command's module is not imported just to describe it. A new top-level
    command adds its entry here.
    """

    target: str
    help: str


# Map each top-level command name to the "module:attribute" of its click
# command object. Imported lazily on dispatch by LazyGroup so the hot
# `winter ws worktrees` path never pays for the `doctor` or `tui` (textual)
# command trees it doesn't touch. Keep this in sync with the command modules.
_LAZY_SUBCOMMANDS: dict[str, LazySubcommand] = {
    "agents": LazySubcommand(
        "winter_cli.modules.agents.command:agents_command",
        (
            "Show the resolved model and effort for every installed agent x harness, plus the tables each one"
            " was resolved from — the same resolution path `winter ws init` bakes into the rendered agent "
            "files."
        ),
    ),
    "capabilities": LazySubcommand(
        "winter_cli.modules.capability.command:capabilities_command",
        (
            "List every known capability slot — its bound provider, other installed candidates, and whether "
            "each candidate's entrypoint resolves."
        ),
    ),
    "clean": LazySubcommand(
        "winter_cli.modules.provision.clean_command:clean_command",
        ("Run each matched provision handler's declared `clean` command across feature envs matched by PATTERNS."),
    ),
    "dashboard": LazySubcommand(
        "winter_cli.modules.tui.command:dashboard",
        "Launch the TUI dashboard.",
    ),
    "doctor": LazySubcommand(
        "winter_cli.modules.doctor.command:doctor_command",
        "Run preflight checks for the workspace and every installed extension.",
    ),
    "env": LazySubcommand(
        "winter_cli.modules.workspace.env_command:env_cmd",
        "Print the runtime environment variables for SCOPE as sourceable export lines.",
    ),
    "ext": LazySubcommand(
        "winter_cli.modules.ext.command:ext_group",
        "Extension management commands.",
    ),
    "graph": LazySubcommand(
        "winter_cli.modules.graph.command:graph_command",
        "Print the module dependency graph declared in `winter-ext.toml` `requires`.",
    ),
    "lint": LazySubcommand(
        "winter_cli.modules.lint.command:lint_command",
        "Run convention lint checks over the project repos in a feature environment.",
    ),
    "provision": LazySubcommand(
        "winter_cli.modules.provision.command:provision_command",
        ("Provision one or more feature envs matched by PATTERNS: install dependencies, create resources, load data."),
    ),
    "service": LazySubcommand(
        "winter_cli.modules.service.command:service_group",
        "Control workspace services via the registered orchestrator extension.",
    ),
    "space": LazySubcommand(
        "winter_cli.modules.space.command:space_command",
        "Print the winter-space directory for artifact KIND (read-only resolution).",
    ),
    "ws": LazySubcommand(
        "winter_cli.modules.workspace.command:ws_group",
        "Manage feature environments.",
    ),
    "repo": LazySubcommand(
        "winter_cli.modules.workspace.command:repo_group",
        "Manage repositories.",
    ),
}


# The literal root token of every command span name (`winter ws init`), whatever the
# process was launched as (`python -m winter_cli.cli`, the shim, ...).
_ROOT_COMMAND_NAME = "winter"

# The logger namespace the OpenTelemetry SDK and exporter log under.
_OPENTELEMETRY_LOGGER = "opentelemetry"

# `Context.meta` key under which the open command span's tracer is parked between
# `resolve_command` (where the span opens) and `invoke` (where it closes).
_OPEN_SPAN_META_KEY = "winter.tracing.open_command_tracer"


def _exit_error_type(exc: BaseException | None) -> str | None:
    """The `error.type` for a command that ended with `exc`, or `None` for a successful exit.

    A command that returns, raises `SystemExit(0)` / `SystemExit(None)`, or raises click's
    `Exit(0)` succeeded. Anything else failed, identified by its exception class name only.
    """
    if exc is None:
        return None
    if isinstance(exc, SystemExit) and exc.code in (None, 0):
        return None
    if isinstance(exc, click.exceptions.Exit) and exc.exit_code == 0:
        return None
    return type(exc).__name__


class LazyGroup(click.Group):
    """A `click.Group` that imports each subcommand's module only when that
    subcommand is dispatched.

    `list_commands` (used by `--help` and shell completion) reports every name
    without importing anything, so `winter --help` still lists all top-level
    commands. `get_command` performs the deferred import for the one command
    actually being run.

    It is also the command span's boundary: the span opens in `resolve_command`
    (the first point where the invoked command's path is known, before the group
    callback runs) and closes in `invoke`. The tracer comes from the `Container`
    the CLI boundary handed in through `ctx.obj`; a tree entered without one
    traces nothing.
    """

    def __init__(self, *args: object, lazy_subcommands: dict[str, LazySubcommand], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._lazy_subcommands = lazy_subcommands

    def list_commands(self, ctx: click.Context) -> list[str]:
        eager = super().list_commands(ctx)
        return sorted([*eager, *self._lazy_subcommands])

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Click's listing, with each lazy command's short help derived from its declared text."""
        names = self.list_commands(ctx)
        if not names:
            return
        limit = formatter.width - 6 - max(len(name) for name in names)
        rows: list[tuple[str, str]] = []
        for name in names:
            if name in self._lazy_subcommands:
                rows.append(
                    (name, click.Command(name, help=self._lazy_subcommands[name].help).get_short_help_str(limit))
                )
            elif (cmd := super().get_command(ctx, name)) is not None and not cmd.hidden:
                rows.append((name, cmd.get_short_help_str(limit)))
        with formatter.section("Commands"):
            formatter.write_dl(rows)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in self._lazy_subcommands:
            return self._load(cmd_name)
        return super().get_command(ctx, cmd_name)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        cmd_name, cmd, remaining = super().resolve_command(ctx, args)
        if cmd is not None and cmd_name is not None and not ctx.resilient_parsing:
            tracer = self._command_tracer(ctx)
            ctx.meta[_OPEN_SPAN_META_KEY] = tracer
            tracer.start_command(" ".join([_ROOT_COMMAND_NAME, *self._command_path(ctx, cmd_name, cmd, remaining)]))
        return cmd_name, cmd, remaining

    def invoke(self, ctx: click.Context) -> object:
        try:
            result = super().invoke(ctx)
        except BaseException as exc:
            self._end_command_span(ctx, _exit_error_type(exc))
            raise
        self._end_command_span(ctx, None)
        return result

    @staticmethod
    def _command_tracer(ctx: click.Context) -> ICommandTracer:
        if isinstance(ctx.obj, CliContext):
            return ctx.obj.container.command_tracer()
        return NoopCommandTracer()

    @staticmethod
    def _command_path(ctx: click.Context, cmd_name: str, cmd: click.Command, remaining: list[str]) -> list[str]:
        """The invoked command's name tokens: the top-level name plus each nested group's subcommand.

        Descent stops at the first token that is not a subcommand name (an option, a
        positional, or an unknown name — click reports those itself), so the path is only ever
        built from names click's own `get_command` resolves.
        """
        path = [cmd_name]
        while isinstance(cmd, click.Group) and remaining and (sub := cmd.get_command(ctx, remaining[0])) is not None:
            path.append(remaining[0])
            cmd, remaining = sub, remaining[1:]
        return path

    @staticmethod
    def _end_command_span(ctx: click.Context, error_type: str | None) -> None:
        tracer: ICommandTracer | None = ctx.meta.pop(_OPEN_SPAN_META_KEY, None)
        if tracer is not None:
            tracer.end_command(error_type)

    def _load(self, cmd_name: str) -> click.Command:
        module_name, _, attr = self._lazy_subcommands[cmd_name].target.partition(":")
        command = getattr(importlib.import_module(module_name), attr)
        if not isinstance(command, click.Command):
            raise TypeError(f"lazy subcommand {cmd_name!r} did not resolve to a click.Command")
        return command


def _configure_logging(verbose: bool, log_level_env: str | None) -> None:
    """Attach a stderr StreamHandler to the winter_cli and opentelemetry loggers.

    Resolution order (first wins):
      1. ``--verbose`` / ``-v`` flag → DEBUG
      2. ``WINTER_LOG_LEVEL`` env var (standard level name, case-insensitive)
      3. Neither set → no handler attached (silent, matching previous behaviour)

    The same handler also serves the ``opentelemetry`` logger, so SDK and exporter
    diagnostics (a failed trace export) surface only under these two switches.

    All diagnostics go to **stderr** so that ``--json`` stdout stays pure JSON.
    """
    if verbose:
        level = logging.DEBUG
    elif log_level_env:
        level = getattr(logging, log_level_env.upper(), None)
        if not isinstance(level, int):
            # Silently ignore an unrecognised level name — don't break the CLI.
            return
    else:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    for logger_name in ("winter_cli", _OPENTELEMETRY_LOGGER):
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        logger.addHandler(handler)


@click.group(cls=LazyGroup, lazy_subcommands=_LAZY_SUBCOMMANDS)
@click.version_option(package_name="winter-cli", message="%(prog)s, version %(version)s")
@click.option("--source-override", default=None, hidden=True)
@click.option(
    "--service-orchestrator",
    default=None,
    metavar="PATH_OR_NAME",
    help=(
        "Override the service orchestrator for this invocation. "
        "A local path (contains a path separator or resolves to an existing directory) "
        "short-circuits the registered-extension lookup and reads that directory's "
        "winter-ext.toml directly. A bare name falls back to the registered-extension "
        "lookup. Takes precedence over WINTER_SERVICE_ORCHESTRATOR and "
        "capabilities.service in .winter/config.toml."
    ),
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help=(
        "Enable DEBUG-level logging on stderr. "
        "Equivalent to WINTER_LOG_LEVEL=DEBUG. "
        "Diagnostics always go to stderr; --json stdout stays pure JSON."
    ),
)
@click.pass_context
def _cli_group(
    ctx: click.Context,
    source_override: str | None,
    service_orchestrator: str | None,
    verbose: bool,
) -> None:
    """Winter — workspace management CLI."""
    # Wire logging before any subcommand runs.
    _configure_logging(verbose, os.environ.get("WINTER_LOG_LEVEL"))

    # Resolve effective orchestrator override: flag > env var > config (config is
    # handled by the resolver itself; we only surface the boundary-level override here).
    effective_orchestrator_override = service_orchestrator or os.environ.get("WINTER_SERVICE_ORCHESTRATOR")

    # The CLI boundary hands in a `CliContext` whose container is built on first need (the
    # command span usually already built it). Adopt it rather than building a second one; a
    # click tree entered without one builds its own, still on first need.
    base = ctx.obj if isinstance(ctx.obj, CliContext) else CliContext(LazyContainer(_default_container))
    ctx.obj = replace(
        base,
        source_override=source_override,
        service_orchestrator_override=effective_orchestrator_override or None,
    )


def _default_container() -> Container:
    from winter_cli.container import Container

    return Container()


def _container_with_tracing(settings: TracingSettings) -> Container:
    """The process's one `Container`, carrying the boundary's tracing settings."""
    from dependency_injector import providers

    from winter_cli.container import Container

    container = Container()
    container.tracing_settings.override(providers.Object(settings))
    return container


def _tracing_settings_from_environment() -> TracingSettings:
    """Read the launch-time tracing environment once, and take it out of the process environment.

    `WINTER_LAUNCH_TIME` describes this process's launch only. It is deleted so a child
    winter that is not launched through the shim never inherits a stale instant.
    """
    raw_launch_time = os.environ.pop("WINTER_LAUNCH_TIME", None)
    return TracingSettings(
        otlp_endpoint=non_blank(os.environ.get("WINTER_OTEL_EXPORTER_OTLP_ENDPOINT")),
        otlp_headers=non_blank(os.environ.get("WINTER_OTEL_EXPORTER_OTLP_HEADERS")),
        sdk_disabled=parse_sdk_disabled(os.environ.get("OTEL_SDK_DISABLED")),
        launch_time_ns=parse_launch_time_ns(raw_launch_time, time.time_ns()),
    )


def _silence_opentelemetry_diagnostics() -> None:
    """Keep the OpenTelemetry SDK's own log records off stderr.

    The SDK logs export failures at ERROR, and with no handler configured Python's last-resort
    handler would print them. A `NullHandler` plus no propagation swallows every record until
    `_configure_logging` routes them to winter's stderr handler under `--verbose` or
    `WINTER_LOG_LEVEL`. Runs before the SDK is imported so no record can slip through earlier.
    """
    sdk_logger = logging.getLogger(_OPENTELEMETRY_LOGGER)
    sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False


def cli() -> None:
    """Process entrypoint — translates RepoError into a clean non-zero exit.

    Click natively handles `ClickException`, but a `RepoError` escaping a
    handler would otherwise dump a traceback. Catch it here and render the
    structured fields (subcommand, args, cwd, exit code, stderr) before
    exiting non-zero — this is the CLI boundary the harness's
    error-handling rules call out.

    Runs `_cli_group.main(standalone_mode=False)` rather than letting Click
    exit on its own, precisely so a `RepoError`/`ConfigError` escaping a
    handler lands here instead of dumping a traceback. Under
    `standalone_mode=False`, an in-command `ctx.exit(n)` does not exit the
    process — Click raises `Exit(n)` internally and `main()` returns `n` from
    `invoke()` (see `click.core.BaseCommand.main`) instead of calling
    `sys.exit`. Every command callback in this CLI implicitly returns `None`
    on success (verified across the whole command surface), so the only two
    shapes `main()` can hand back are `None` (success) and an `int` (an
    in-command `ctx.exit(n)`) — propagate the latter to the shell.
    """
    tracing_settings = _tracing_settings_from_environment()

    # Pave SSH-side keepalives into GIT_SSH_COMMAND so a wedged TCP socket
    # surfaces as an SSH error in ~90s instead of relying solely on the
    # per-call Python-side timeout. Idempotent and respects user overrides.
    # NB: runs before Click parses argv, so even `winter --help` and a
    # future `winter doctor` probe will see the paved default. If a probe
    # ever wants to report on the raw user-set GIT_SSH_COMMAND, it must
    # snapshot the env before this call rather than reading at probe time.
    ensure_ssh_keepalives()

    # The container is built on first need — normally the tracer lookup in `LazyGroup` as it
    # opens the command span — so `--version` and `--help` never import it. The group callback
    # adopts this same instance into `CliContext`.
    _silence_opentelemetry_diagnostics()
    container_source = LazyContainer(lambda: _container_with_tracing(tracing_settings))
    try:
        exit_code = _cli_group.main(standalone_mode=False, obj=CliContext(container_source))
        if isinstance(exit_code, int):
            sys.exit(exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        # Imported here: the models module pulls in pydantic, which only a failing run needs.
        from winter_cli.modules.workspace.models import RepoError

        if not isinstance(exc, RepoError):
            raise
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    finally:
        # The single exit choke point: every `sys.exit` above and in handlers, every
        # `ctx.exit`, and an uncaught exception all pass through here exactly once.
        if container_source.built:
            container_source.get().command_tracer().export()


if __name__ == "__main__":
    cli()
