from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


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

from winter_cli.cli_context import CliContext
from winter_cli.core.config_file import ConfigError
from winter_cli.modules.workspace.models import RepoError

# Map each top-level command name to the "module:attribute" of its click
# command object. Imported lazily on dispatch by LazyGroup so the hot
# `winter ws worktrees` path never pays for the `doctor` or `tui` (textual)
# command trees it doesn't touch. Keep this in sync with the command modules.
_LAZY_SUBCOMMANDS: dict[str, str] = {
    "capabilities": "winter_cli.modules.capability.command:capabilities_command",
    "dashboard": "winter_cli.modules.tui.command:dashboard",
    "doctor": "winter_cli.modules.doctor.command:doctor_command",
    "ext": "winter_cli.modules.ext.command:ext_group",
    "graph": "winter_cli.modules.graph.command:graph_command",
    "lint": "winter_cli.modules.lint.command:lint_command",
    "service": "winter_cli.modules.service.command:service_group",
    "ws": "winter_cli.modules.workspace.command:ws_group",
    "repo": "winter_cli.modules.workspace.command:repo_group",
}


class LazyGroup(click.Group):
    """A `click.Group` that imports each subcommand's module only when that
    subcommand is dispatched.

    `list_commands` (used by `--help` and shell completion) reports every name
    without importing anything, so `winter --help` still lists all top-level
    commands. `get_command` performs the deferred import for the one command
    actually being run.
    """

    def __init__(self, *args: object, lazy_subcommands: dict[str, str], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._lazy_subcommands = lazy_subcommands

    def list_commands(self, ctx: click.Context) -> list[str]:
        eager = super().list_commands(ctx)
        return sorted([*eager, *self._lazy_subcommands])

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in self._lazy_subcommands:
            return self._load(cmd_name)
        return super().get_command(ctx, cmd_name)

    def _load(self, cmd_name: str) -> click.Command:
        module_name, _, attr = self._lazy_subcommands[cmd_name].partition(":")
        command = getattr(importlib.import_module(module_name), attr)
        if not isinstance(command, click.Command):
            raise TypeError(f"lazy subcommand {cmd_name!r} did not resolve to a click.Command")
        return command


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
        "service_orchestrator in .winter/config.toml."
    ),
)
@click.pass_context
def _cli_group(ctx: click.Context, source_override: str | None, service_orchestrator: str | None):
    """Winter — workspace management CLI."""
    from winter_cli.container import Container

    # Resolve effective orchestrator override: flag > env var > config (config is
    # handled by the resolver itself; we only surface the boundary-level override here).
    effective_orchestrator_override = service_orchestrator or os.environ.get("WINTER_SERVICE_ORCHESTRATOR")

    ctx.obj = CliContext(
        container=Container(),
        source_override=source_override,
        service_orchestrator_override=effective_orchestrator_override or None,
    )


def cli() -> None:
    """Process entrypoint — translates RepoError into a clean non-zero exit.

    Click natively handles `ClickException`, but a `RepoError` escaping a
    handler would otherwise dump a traceback. Catch it here and render the
    structured fields (subcommand, args, cwd, exit code, stderr) before
    exiting non-zero — this is the CLI boundary the harness's
    error-handling rules call out.
    """
    # Pave SSH-side keepalives into GIT_SSH_COMMAND so a wedged TCP socket
    # surfaces as an SSH error in ~90s instead of relying solely on the
    # per-call Python-side timeout. Idempotent and respects user overrides.
    # NB: runs before Click parses argv, so even `winter --help` and a
    # future `winter doctor` probe will see the paved default. If a probe
    # ever wants to report on the raw user-set GIT_SSH_COMMAND, it must
    # snapshot the env before this call rather than reading at probe time.
    from winter_cli.modules.workspace.internal.git_ops_service import ensure_ssh_keepalives

    ensure_ssh_keepalives()
    try:
        _cli_group.main(standalone_mode=False)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except RepoError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
