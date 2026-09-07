"""Pins `winter ws restack`'s command-body → `RestackParams` wiring.

`ws_restack`'s body does `*chain, base = args` and forwards `cut` /
`dry_run` / `output_json` straight into `RestackParams` — nothing before
this test ever constructed a container and drove that wiring end to end
(`test_restack_command_cli.py` deliberately stops short of it, invoking the
command with no `obj` at all, since its own checks raise before
`cli_ctx(ctx).container` is ever touched). Left unpinned, a transposed
`dry_run=output_json, output_json=dry_run` type-checks and leaves the whole
suite green while `--dry-run` — the safety mechanism for the first
history-rewriting verb this CLI has — would silently execute real rebases.

Follows `test_provision_command_handler.py`'s idiom for exactly this shape
of gap: stub the container so the command runs past `cli_ctx(ctx).container`
for real, and assert on the `RestackParams` a capturing fake handler
actually received, rather than mocking `RestackHandler` itself (a mock's
`assert_called_with` would still pass on a transposition, since both
booleans are valid arguments to the same keyword).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner, Result

from winter_cli.cli_context import CliContext
from winter_cli.modules.workspace.command import ws_restack
from winter_cli.modules.workspace.handlers.restack_handler import RestackParams


class _CapturingHandler:
    """Records the `RestackParams` it was actually called with, in argument
    order — never resolves anything else in the container, so a wrong wire
    into some other collaborator would surface as a `MagicMock` attribute
    error instead of a silent pass."""

    def __init__(self) -> None:
        self.calls: list[RestackParams] = []

    def run(self, params: RestackParams) -> None:
        self.calls.append(params)


def _invoke(args: list[str]) -> tuple[_CapturingHandler, Result]:
    handler = _CapturingHandler()
    container = MagicMock()
    container.restack_handler.return_value = handler
    ctx = CliContext(container=container)
    result = CliRunner().invoke(ws_restack, args, obj=ctx, catch_exceptions=False)
    return handler, result


def test_dry_run_alone_wires_dry_run_true_and_output_json_false() -> None:
    """The half of a `dry_run`/`output_json` transposition a `--json`-only
    test below can't catch: passing only `--dry-run` must not also flip
    `output_json` true."""
    handler, result = _invoke(["env1", "master", "--dry-run"])

    assert result.exit_code == 0
    (params,) = handler.calls
    assert params.dry_run is True
    assert params.output_json is False


def test_json_alone_wires_output_json_true_and_dry_run_false() -> None:
    """The other half: passing only `--json` must not also flip `dry_run`
    true — a transposed wire would pass `test_dry_run_alone...` and this one
    only if it swapped both directions, which a keyword-argument
    transposition always does by construction; each test alone pins one
    direction of the swap."""
    handler, result = _invoke(["env1", "master", "--json"])

    assert result.exit_code == 0
    (params,) = handler.calls
    assert params.dry_run is False
    assert params.output_json is True


def test_multi_link_chain_and_cut_wire_in_argument_order() -> None:
    """`*chain, base = args` — every positional but the last becomes a
    chain element in the order typed, the last becomes `base`; `--cut`
    forwards unchanged."""
    handler, result = _invoke(["env3", "env2", "master", "--cut", "old-env"])

    assert result.exit_code == 0
    (params,) = handler.calls
    assert params.chain == ["env3", "env2"]
    assert params.base == "master"
    assert params.cut == "old-env"


def test_omitted_cut_and_flags_wire_falsy_defaults() -> None:
    handler, result = _invoke(["env1", "master"])

    assert result.exit_code == 0
    (params,) = handler.calls
    assert params.chain == ["env1"]
    assert params.base == "master"
    assert params.cut is None
    assert params.dry_run is False
    assert params.output_json is False
