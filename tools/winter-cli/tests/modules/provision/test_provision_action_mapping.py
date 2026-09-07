"""Tests that `provision_command` and `clean_command` build the right `ProvisionAction`.

Covers the CLI-flag → `ProvisionParams.action` translation for both commands via
a fake `provision_command_handler`, so a regression that mis-maps a flag (or
that has `clean_command` fall back to `ProvisionAction.apply`) fails here
instead of silently shipping a `winter clean` that provisions the environment.

The same wiring pins `clean`'s ungated posture: reaching the fake handler at all
proves no prompt stood in the way.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

from winter_cli.cli_context import CliContext
from winter_cli.modules.provision.clean_command import clean_command
from winter_cli.modules.provision.command import provision_command
from winter_cli.modules.provision.handler import ProvisionParams
from winter_cli.modules.provision.manifest import ProvisionAction


class _RecordingProvisionCommandHandler:
    """Records the `ProvisionParams` passed to `run()`; does no real work."""

    def __init__(self) -> None:
        self.calls: list[ProvisionParams] = []

    def run(self, params: ProvisionParams) -> None:
        self.calls.append(params)


def _make_ctx() -> tuple[CliContext, _RecordingProvisionCommandHandler]:
    fake_handler = _RecordingProvisionCommandHandler()
    container = MagicMock()
    container.provision_command_handler.return_value = fake_handler
    return CliContext(container=container), fake_handler


class TestProvisionCommandActionMapping:
    def test_bare_invocation_maps_to_apply(self) -> None:
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(provision_command, ["alpha"], obj=ctx, catch_exceptions=False)

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
        assert fake_handler.calls[0].action is ProvisionAction.apply

    def test_reset_flag_maps_to_reset(self) -> None:
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(
            provision_command, ["alpha", "--stage", "resource", "--reset"], obj=ctx, catch_exceptions=False
        )

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
        assert fake_handler.calls[0].action is ProvisionAction.reset

    def test_destroy_flag_maps_to_destroy(self) -> None:
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(
            provision_command, ["alpha", "--stage", "resource", "--destroy"], obj=ctx, catch_exceptions=False
        )

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
        assert fake_handler.calls[0].action is ProvisionAction.destroy


class TestCleanCommandActionMapping:
    def test_clean_invocation_maps_to_clean_not_apply(self) -> None:
        """`clean_command` must build `ProvisionParams(action=ProvisionAction.clean, ...)`.

        If this regressed to `ProvisionAction.apply`, `winter clean` would
        provision the environment instead of running each handler's declared
        `clean` command — and no other test in the suite would catch it.
        """
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(clean_command, ["alpha"], obj=ctx, catch_exceptions=False)

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
        assert fake_handler.calls[0].action is ProvisionAction.clean

    def test_clean_invocation_with_stage_and_name_still_maps_to_clean(self) -> None:
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(
            clean_command,
            ["alpha", "--stage", "resource", "--name", "workspace.mydb"],
            obj=ctx,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
        assert fake_handler.calls[0].action is ProvisionAction.clean
        assert fake_handler.calls[0].subtarget == "resource"
        assert fake_handler.calls[0].name_selector == "workspace.mydb"


class TestCleanCommandIsUngated:
    def test_runs_to_the_handler_with_stdin_empty(self) -> None:
        """`clean` must issue no confirmation prompt.

        A `click.confirm` anywhere ahead of the handler aborts the run when
        stdin supplies no answer, so reaching the handler with `input=""` is
        what proves the absence — independently of how any future prompt might
        be worded. Asserting instead that some specific prompt text is missing
        would pass the moment someone phrased one differently.
        """
        ctx, fake_handler = _make_ctx()
        result = CliRunner().invoke(clean_command, ["alpha"], obj=ctx, input="", catch_exceptions=False)

        assert result.exit_code == 0
        assert len(fake_handler.calls) == 1
