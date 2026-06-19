from __future__ import annotations

# Drift guard: winter service subcommands must match the service-v1 spec.
#
# Each action declared in service-v1.toml must have a registered Click subcommand
# with a matching short_help summary.  If someone edits the spec or the command
# module out of agreement this test fails, preventing silent drift between the
# machine-readable contract and the user-facing help text.
import click
import pytest

from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader
from winter_cli.modules.capability.spec_loader import SpecLoader
from winter_cli.modules.service.command import service_group


def _real_spec_loader() -> SpecLoader:
    return SpecLoader(config_file_reader=TomllibConfigFileReader())


def _spec_actions() -> dict[str, str]:
    """Return {name: summary} for every action in the bundled service-v1 spec."""
    spec = _real_spec_loader().load("service", "v1")
    return {a.name: a.summary for a in spec.actions}


def _click_subcommands() -> dict[str, click.Command]:
    """Return {name: command} for every subcommand registered on service_group."""
    ctx = click.Context(service_group)
    return {name: service_group.get_command(ctx, name) for name in service_group.list_commands(ctx)}  # type: ignore[misc]


# ── action set parity ────────────────────────────────────────────────────────


def test_service_subcommand_names_match_spec_action_names() -> None:
    """Every spec action has a click subcommand; no extra subcommands exist."""
    spec_names = set(_spec_actions())
    click_names = set(_click_subcommands())
    assert click_names == spec_names, (
        f"Click subcommands {sorted(click_names)} do not match spec actions {sorted(spec_names)}. "
        "Update service/command.py or service-v1.toml to bring them back in sync."
    )


# ── summary / short_help parity ──────────────────────────────────────────────


@pytest.mark.parametrize("action_name", sorted(_spec_actions()))
def test_service_subcommand_short_help_matches_spec_summary(action_name: str) -> None:
    """Each subcommand's short_help must equal the spec's summary for that action."""
    spec_summary = _spec_actions()[action_name]
    cmd = _click_subcommands()[action_name]
    assert cmd is not None, f"Click subcommand {action_name!r} not found"

    # Click truncates short_help to 45 chars by default.  Compare the full
    # make_short_help expansion (which preserves the original) rather than
    # comparing against a truncated string.
    short_help = cmd.get_short_help_str(limit=200)
    assert short_help == spec_summary, (
        f"service {action_name!r}: short_help {short_help!r} does not match "
        f"spec summary {spec_summary!r}. Update service/command.py to source the "
        "short_help from _SPEC_SUMMARIES, or update service-v1.toml."
    )
