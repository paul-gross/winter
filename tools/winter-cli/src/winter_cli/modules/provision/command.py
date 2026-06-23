from __future__ import annotations

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.provision.handler import ProvisionParams


@click.command("provision")
@click.argument("env")
@click.argument("subtarget", required=False, type=click.Choice(["dependency", "resource", "data"]))
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Reset the sub-target (destroy + recreate, or dedicated reset handler).",
)
@click.option("--destroy", is_flag=True, default=False, help="Destroy the sub-target only.")
@click.option("--seed", is_flag=True, default=False, help="Create resources then seed data (resource only).")
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
    env: str,
    subtarget: str | None,
    reset: bool,
    destroy: bool,
    seed: bool,
    no_service_check: bool,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Provision feature env ENV: install dependencies, create resources, load data.

    Runs three ordered sub-targets — dependency → resource → data — or a
    single explicit SUBTARGET.

    \b
    Examples:
      winter provision alpha                     # full chain
      winter provision alpha dependency          # install dependencies only
      winter provision alpha resource --reset    # destroy + recreate resources
      winter provision alpha resource --destroy  # destroy resources only
      winter provision alpha resource --seed     # create resources + seed data
      winter provision alpha data                # load baseline state
      winter provision alpha --json              # full chain, NDJSON output
      winter provision alpha --dry-run           # print plan, no side effects
      winter provision alpha --dry-run --json    # structured plan as NDJSON
    """
    # Validate mutually exclusive action flags.
    if reset and destroy:
        raise click.ClickException("--reset and --destroy are mutually exclusive")

    # --seed is only valid for the resource sub-target with no other action flag.
    if seed:
        if subtarget != "resource":
            raise click.ClickException("--seed requires an explicit 'resource' sub-target")
        if reset or destroy:
            raise click.ClickException("--seed cannot be combined with --reset or --destroy")

    # Action flags require an explicit sub-target (the full chain doesn't accept
    # a single action because dependency/resource/data use them differently).
    if (reset or destroy) and subtarget is None:
        raise click.ClickException("--reset and --destroy require an explicit SUBTARGET")

    container = cli_ctx(ctx).container
    handler = container.provision_command_handler()
    handler.run(
        ProvisionParams(
            env=env,
            subtarget=subtarget,
            reset=reset,
            destroy=destroy,
            seed=seed,
            no_service_check=no_service_check,
            dry_run=dry_run,
            output_json=output_json,
        )
    )
