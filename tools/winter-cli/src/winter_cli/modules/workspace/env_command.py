"""``winter env`` — print the computed runtime environment for a scope.

Usage::

    winter env <scope> [--resolve]

*scope* is a feature-env name (e.g. ``alpha``) or the reserved literal
``workspace``.  The command prints one ``export KEY=value`` line per
variable in the order the provisioner returns them:

    export WINTER_ENV=alpha
    export WINTER_ENV_INDEX=1
    export WINTER_PORT_BASE=4060
    export WINTER_WORKSPACE_PORT_BASE=4000
    export MY_APP_PORT=4061           # from [env.feature.vars] if declared

Without ``--resolve``, a command entry's key is never run — it prints with the
resolver's placeholder value (``COMMAND_PLACEHOLDER`` in
``env_band_resolver_service.py``) instead, keeping ``winter env`` pure,
offline, and safe to print. With
``--resolve``, every command entry runs (once its references resolve) and its
real value is printed.

The output is designed to be shell-sourced::

    source <(winter env alpha)     # bash/zsh
    . (winter env alpha | psub)    # fish

Exit codes:

- 0 — success.
- 1 — unknown scope (no allocation for the given env name), misconfigured
      env-band template, a command entry that fails under ``--resolve``
      (non-zero exit, timeout, or unparseable output), or other fatal error.
"""

from __future__ import annotations

import shlex

import click

from winter_cli.cli_context import cli_ctx


@click.command("env")
@click.argument("scope")
@click.option(
    "--resolve",
    "resolve",
    is_flag=True,
    default=False,
    help="Run command entries and print their real values instead of the placeholder.",
)
@click.pass_context
def env_cmd(ctx: click.Context, scope: str, resolve: bool) -> None:
    """Print the runtime environment variables for SCOPE as sourceable export lines.

    SCOPE is a feature-env name (e.g. ``alpha``) or ``workspace``.  The output
    can be shell-sourced to inject WINTER_* and env-band variables into the
    current shell session::

        source <(winter env alpha)

    Without ``--resolve``, a command entry never runs — its key prints with a
    placeholder value. With ``--resolve``, command entries run for real; a
    failing one exits 1.
    """
    container = cli_ctx(ctx).container
    if scope != "workspace" and scope not in container.env_index_registry().all_assignments():
        click.echo(
            f"winter env: unknown scope {scope!r} — no env by that name is registered",
            err=True,
        )
        ctx.exit(1)
        return
    provisioner = container.env_provisioner()
    try:
        env_map = provisioner.compute(scope, resolve_commands=resolve)
    except ValueError as exc:
        click.echo(f"winter env: error computing environment for {scope!r}: {exc}", err=True)
        ctx.exit(1)
        return
    for key, value in env_map.items():
        click.echo(f"export {key}={shlex.quote(value)}")
