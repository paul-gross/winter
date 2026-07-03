from __future__ import annotations

import os
from pathlib import Path

import click

from winter_cli.cli_context import cli_ctx
from winter_cli.modules.ext.models import NewParams, VerifyParams


def resolve_output_dir(invocation_cwd: Path, name: str, output_dir: str | None) -> Path:
    """Resolve the output directory for `ext new`.

    - ``output_dir`` is None   → ``invocation_cwd / name``  (default)
    - ``output_dir`` is absolute → that path as-is
    - ``output_dir`` is relative → ``invocation_cwd / output_dir``
    """
    if output_dir is None:
        return invocation_cwd / name
    p = Path(output_dir)
    if p.is_absolute():
        return p
    return invocation_cwd / p


@click.group("ext")
def ext_group() -> None:
    """Extension management commands."""


@ext_group.command("verify")
@click.argument("extensions", nargs=-1, required=True)
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit results as JSON.")
@click.pass_context
def verify_cmd(ctx: click.Context, extensions: tuple[str, ...], output_json: bool) -> None:
    """Verify that each EXTENSION conforms to the service capability spec.

    Each EXTENSION is either the name of an installed standalone extension (as
    declared in .winter/config.toml) or a local path to an extension
    directory. The extension's winter-ext.toml must declare a service
    entrypoint via `orchestrate_services` or `[provides] service`. Pass any
    number of EXTENSIONs to verify them all in one run — there is no glob
    support here (a name/path isn't a registry enumeration to expand).

    Runs golden invocations from the bundled service spec and reports each
    check as a pass or fail. Exits non-zero if any check fails or setup fails
    for any of the given EXTENSIONs.

    \b
      winter ext verify my-ext                  # verify one extension
      winter ext verify my-ext other-ext         # verify two, in one run
      winter ext verify my-ext --json            # results as JSON
    """
    container = cli_ctx(ctx).container
    handler = container.ext_verify_handler()
    handler.run(VerifyParams(extensions=list(extensions), output_json=output_json))


@ext_group.command("new")
@click.argument("name")
@click.option("--capability", "slot", required=True, help="Capability slot to implement (e.g. 'service').")
@click.option(
    "--dir",
    "output_dir",
    default=None,
    help=(
        "Output directory (default: <current-directory>/<name>/)."
        " A relative path is resolved against the current directory."
    ),
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing non-empty output directory.")
@click.pass_context
def new_cmd(ctx: click.Context, name: str, slot: str, output_dir: str | None, force: bool) -> None:
    """Scaffold a new extension named NAME that implements a capability slot.

    Generates a winter-ext.toml, an index.md skeleton, and a refuse-all stub
    entrypoint that passes `winter ext verify` out of the box.  The action set
    and exit codes in the stub are derived from the bundled capability spec so
    the scaffold and the verifier always agree.

    The output directory defaults to <current-directory>/<name>/; override with
    --dir (absolute path used as-is; relative path resolved against the current
    directory).  Pass --force to allow writing into a non-empty existing directory.
    """
    container = cli_ctx(ctx).container

    # Validate the slot before touching the filesystem.
    spec_loader = container.spec_loader()
    supported = spec_loader.supported_versions(slot)
    if not supported:
        raise click.UsageError(
            f"unknown capability slot {slot!r} — no spec found. "
            "Available slots are derived from the bundled specs in winter-cli."
        )

    # Read the invocation cwd at the thin click boundary — WINTER_INVOCATION_CWD is
    # set by the bin/winter launcher before it pins cwd to tools/winter-cli/ for
    # gitpython. Fall back to Path.cwd() for direct invocation and tests.
    invocation_cwd = Path(os.environ.get("WINTER_INVOCATION_CWD") or Path.cwd())
    resolved_dir = resolve_output_dir(invocation_cwd, name, output_dir)

    handler = container.ext_new_handler()
    handler.run(NewParams(name=name, slot=slot, output_dir=resolved_dir, force=force))
