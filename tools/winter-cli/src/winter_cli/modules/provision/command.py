from __future__ import annotations

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.provision.handler import ProvisionParams
from winter_cli.modules.provision.manifest import ProvisionAction
from winter_cli.modules.workspace.pattern_match import validate_env_pattern


@click.command("provision")
@click.argument("patterns", nargs=-1, required=True)
@click.option(
    "--stage",
    "subtarget",
    type=click.Choice(["dependency", "resource", "data"]),
    default=None,
    help="Run a single sub-target instead of the full dependency → resource → data chain.",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Reset the sub-target (destroy + recreate, or dedicated reset handler).",
)
@click.option("--destroy", is_flag=True, default=False, help="Destroy the sub-target only.")
@click.option("--seed", is_flag=True, default=False, help="Create resources then seed data (resource only).")
@click.option(
    "--name",
    "name_selector",
    default=None,
    metavar="SCOPE.NAME",
    help=(
        "Target a single named provision entry instead of a whole stage. "
        "SCOPE.NAME is scope-qualified: SCOPE is one of 'workspace', 'feature', 'worktree'. "
        "Works with bare apply, --destroy, and --reset; --stage is optional when --name is given."
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
    help="Print the ordered list of handlers that would run; no scripts are executed, no services are started.",
)
@click.option(
    "--json", "output_json", is_flag=True, default=False, help="Emit NDJSON events instead of human-readable output."
)
@click.pass_context
def provision_command(
    ctx: click.Context,
    patterns: tuple[str, ...],
    subtarget: str | None,
    reset: bool,
    destroy: bool,
    seed: bool,
    name_selector: str | None,
    no_service_check: bool,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Provision one or more feature envs matched by PATTERNS: install dependencies, create resources, load data.

    Each PATTERN is a bare glob over env names — provision operates on whole
    envs, so unlike `ws fetch`/`ws diff`/etc. there is no `<env>/<repo>`
    segment. At least one PATTERN is required — pass a literal name, several
    names, or a glob to fan out across every matching env. Runs three ordered
    sub-targets — dependency → resource → data — per matched env, or a single
    explicit --stage, or a single named entry via --name.

    \b
    Examples:
      winter provision alpha                     # full chain
      winter provision alpha beta                # full chain, two envs
      winter provision 'feature-*'                # full chain, every env matching the glob
      winter provision alpha --stage dependency  # install dependencies only
      winter provision alpha --stage resource --reset    # destroy + recreate resources
      winter provision alpha --stage resource --destroy  # destroy resources only
      winter provision alpha --stage resource --seed     # create resources + seed data
      winter provision alpha --stage data        # load baseline state
      winter provision alpha --json              # full chain, NDJSON output
      winter provision alpha --dry-run           # print plan, no side effects
      winter provision alpha --dry-run --json    # structured plan as NDJSON
      winter provision alpha --name workspace.mydb            # apply just the named entry
      winter provision alpha --name workspace.mydb --reset     # reset just the named entry
      winter provision alpha --name workspace.mydb --destroy   # destroy just the named entry
    """
    # Validate mutually exclusive action flags.
    if reset and destroy:
        raise click.ClickException("--reset and --destroy are mutually exclusive")

    # --seed is only valid for the resource sub-target with no other action flag.
    if seed:
        if subtarget != "resource":
            raise click.ClickException("--seed requires an explicit --stage resource")
        if reset or destroy:
            raise click.ClickException("--seed cannot be combined with --reset or --destroy")
        if name_selector is not None:
            raise click.ClickException("--seed cannot be combined with --name")

    # Action flags require an explicit sub-target or a --name selector (the full
    # chain doesn't accept a single action because dependency/resource/data use
    # them differently) — a --name selector already narrows to one handler, so
    # it satisfies the same requirement without --stage.
    if (reset or destroy) and subtarget is None and name_selector is None:
        raise click.ClickException("--reset and --destroy require an explicit --stage or --name")

    for pattern in patterns:
        validate_env_pattern(pattern)

    action = ProvisionAction.destroy if destroy else (ProvisionAction.reset if reset else ProvisionAction.apply)

    container = cli_ctx(ctx).container
    handler = container.provision_command_handler()
    handler.run(
        ProvisionParams(
            patterns=list(patterns),
            subtarget=subtarget,
            action=action,
            seed=seed,
            no_service_check=no_service_check,
            dry_run=dry_run,
            output_json=output_json,
            name_selector=name_selector,
        )
    )
