from __future__ import annotations

import os
from pathlib import Path

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.lint.handler import LintParams
from winter_cli.modules.lint.models import LintScopeError, LintScopeRequest
from winter_cli.modules.workspace.pattern_match import validate_bare_name_pattern


@click.command("lint")
@click.argument("scopes", nargs=-1)
@click.option("--all", "all_flag", is_flag=True, default=False, help="Lint every feature environment's project repos.")
@click.option("--changed", is_flag=True, default=False, help="Lint only the dirty / un-pushed files.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit NDJSON lint events instead of a table.")
@click.pass_context
def lint_command(ctx: click.Context, scopes: tuple[str, ...], all_flag: bool, changed: bool, output_json: bool) -> None:
    """Run convention lint checks over the project repos in a feature environment.

    `winter lint` is a dispatcher — it runs winter's built-in core checks plus
    each installed extension's contributed `lint` script (and an optional
    workspace-level one) over the selected scope, and aggregates the findings.
    It targets the project repos we develop in feature environments, never the
    workspace root or the standalone extension clones.

    With no SCOPES it lints the feature environment you're standing in (or
    every env when run from outside one). Each SCOPE is a project-repo name,
    an env name, or a bare glob over either (no `<env>/<repo>` segment); pass
    any number to lint exactly that set, in one run. --all lints every env's
    project repos, --changed the dirty / un-pushed file set — both are
    mutually exclusive with SCOPES. A name that matches both a repo and an env
    is rejected as ambiguous. Each finding reports pass / warn / fail with an
    optional file:line location. Exit code is 0 when nothing failed (warnings
    allowed), 1 when any check failed anywhere in scope.

    \b
      winter lint                  # the env you're standing in (or every env)
      winter lint alpha            # one env's project repos
      winter lint alpha beta       # two envs
      winter lint 'winter-*'       # every repo/env name matching the glob
      winter lint --all            # every env's project repos
      winter lint --changed        # dirty / un-pushed files only
    """
    for scope in scopes:
        validate_bare_name_pattern(scope)
    container = cli_ctx(ctx).container
    handler = container.lint_handler()
    # The launcher pins Python's cwd to tools/winter-cli/; WINTER_INVOCATION_CWD
    # carries the caller's real directory, used to detect the current env.
    invocation_cwd = Path(os.environ.get("WINTER_INVOCATION_CWD") or Path.cwd())
    request = LintScopeRequest(names=list(scopes), all=all_flag, changed=changed, cwd=invocation_cwd)
    try:
        handler.run(LintParams(scope=request, output_json=output_json))
    except LintScopeError as exc:
        raise click.ClickException(str(exc)) from exc
