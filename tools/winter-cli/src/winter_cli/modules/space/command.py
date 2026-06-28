"""``winter space`` — resolve a winter-space artifact directory from config.

Usage::

    winter space <kind>

Print the absolute directory the winter space resolves *kind* to. This is a
**pure, read-only** resolution: it reads the ``[space]`` config and prints a
path. It does not create the directory, write anything, or touch git — the
caller owns those. The winter space is where winter and its extensions write
*generated artifacts* (harness scores, review manifests, workflow docs, logs);
a consuming skill reads the resolved value instead of hardcoding a path into one
code harness's home directory.

The single printed line is meant to be captured; create the directory yourself
when you need it::

    dir="$(winter space scores)"     # -> <workspace>/.winter/scores by default
    mkdir -p "$dir"

*kind* is a dynamic, extension-defined bucket name — a single segment starting
with a letter or digit, then letters, digits, ``.``, ``_``, or ``-`` (not a
path), so the resolved directory cannot escape the space root via the argument.

Exit codes:

- 0 — success; the absolute directory is written to stdout (one line).
- 1 — malformed *kind* (path separators / traversal), or a fatal config error.
"""

from __future__ import annotations

import re

import click

from winter_cli.cli_context import cli_ctx

# A kind is a single directory segment, not a path: reject separators and
# traversal so `winter space ../../etc` cannot escape the space root via the
# argument. Override *values* in `[space.kinds]` may be paths (trusted config);
# the kind *name* on the command line may not.
_SAFE_KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@click.command("space")
@click.argument("kind")
@click.pass_context
def space_command(ctx: click.Context, kind: str) -> None:
    """Print the winter-space directory for artifact KIND (read-only resolution).

    KIND is a dynamic, extension-defined bucket name (e.g. ``scores``,
    ``manifests``). Resolves the directory from the ``[space]`` config and writes
    its absolute path to stdout. Creates nothing — the caller materializes the
    directory if and when it writes into it.
    """
    if not _SAFE_KIND.match(kind):
        click.echo(
            f"winter space: invalid kind {kind!r} — a kind is a single name "
            f"(letters, digits, '.', '_', '-'), not a path.",
            err=True,
        )
        ctx.exit(1)
        return

    config = cli_ctx(ctx).container.workspace_config()
    click.echo(str(config.space_dir(kind)))
