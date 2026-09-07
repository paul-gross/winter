from __future__ import annotations

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.provision.handler import ProvisionParams
from winter_cli.modules.provision.manifest import ProvisionAction
from winter_cli.modules.workspace.pattern_match import validate_env_pattern


@click.command("clean")
@click.argument("patterns", nargs=-1, required=True)
@click.option(
    "--stage",
    "subtarget",
    type=click.Choice(["dependency", "resource", "data"]),
    default=None,
    help="Run a single sub-target instead of the full dependency → resource → data chain.",
)
@click.option(
    "--name",
    "name_selector",
    default=None,
    metavar="SCOPE.NAME",
    help=(
        "Target a single named provision entry instead of a whole stage. "
        "SCOPE.NAME is scope-qualified: SCOPE is one of 'workspace', 'feature', 'worktree'. "
        "--stage is optional when --name is given."
    ),
)
@click.option(
    "--no-service-check", is_flag=True, default=False, help="Skip the required-services check before running handlers."
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the ordered list of handlers that would clean; no commands are executed, no services are started.",
)
@click.option(
    "--json", "output_json", is_flag=True, default=False, help="Emit NDJSON events instead of human-readable output."
)
@click.pass_context
def clean_command(
    ctx: click.Context,
    patterns: tuple[str, ...],
    subtarget: str | None,
    name_selector: str | None,
    no_service_check: bool,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Run each matched provision handler's declared `clean` command across feature envs matched by PATTERNS.

    Each PATTERN is a bare glob over env names — clean operates on whole envs,
    like `provision`, so unlike `ws fetch`/`ws diff`/etc. there is no
    `<env>/<repo>` segment. At least one PATTERN is required. Runs the full
    dependency → resource → data chain per matched env, or a single explicit
    --stage, or a single named entry via --name. A handler declaring no
    `clean` command contributes nothing and is not an error; a sub-target
    where nothing declares one reports no handlers and starts no service.

    This is `winter clean`, not `winter ws clean`: this verb runs the
    project-declared `clean` command each handler names in its own manifest
    entry; `winter ws clean` is a git-level `git clean -fd` over a worktree's
    untracked files and knows nothing about the provision manifest.

    \b
    Examples:
      winter clean alpha                     # full chain
      winter clean alpha beta                # full chain, two envs
      winter clean 'feature-*'                # full chain, every env matching the glob
      winter clean alpha --stage resource    # clean the resource sub-target only
      winter clean alpha --name workspace.mydb            # clean just the named entry
      winter clean alpha --json              # full chain, NDJSON output
      winter clean alpha --dry-run           # print plan, no side effects
      winter clean alpha --dry-run --json    # structured plan as NDJSON
    """
    for pattern in patterns:
        validate_env_pattern(pattern)

    container = cli_ctx(ctx).container
    handler = container.provision_command_handler()
    handler.run(
        ProvisionParams(
            patterns=list(patterns),
            subtarget=subtarget,
            action=ProvisionAction.clean,
            seed=False,
            no_service_check=no_service_check,
            dry_run=dry_run,
            output_json=output_json,
            name_selector=name_selector,
        )
    )
