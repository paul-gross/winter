from __future__ import annotations

import sys

# Don't write .pyc files. Plugins are loaded via importlib from inside
# standalone extension repos; without this, every winter run scribbles
# __pycache__/ into the extension's source tree.
sys.dont_write_bytecode = True

import click

from winter_cli.cli_context import CliContext
from winter_cli.modules.tui.command import dashboard
from winter_cli.modules.workspace.command import ws_group, repo_group
from winter_cli.modules.workspace.models import RepoError


@click.group()
@click.option("--source-override", default=None, hidden=True)
@click.pass_context
def _cli_group(ctx: click.Context, source_override: str | None):
    """Winter — workspace management CLI."""
    from winter_cli.container import Container

    ctx.obj = CliContext(container=Container(), source_override=source_override)


_cli_group.add_command(dashboard)
_cli_group.add_command(ws_group)
_cli_group.add_command(repo_group)


def cli() -> None:
    """Process entrypoint — translates RepoError into a clean non-zero exit.

    Click natively handles `ClickException`, but a `RepoError` escaping a
    handler would otherwise dump a traceback. Catch it here and render the
    structured fields (subcommand, args, cwd, exit code, stderr) before
    exiting non-zero — this is the CLI boundary the harness's
    error-handling rules call out.
    """
    try:
        _cli_group.main(standalone_mode=False)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except RepoError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
