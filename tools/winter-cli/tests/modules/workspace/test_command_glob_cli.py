"""CLI argument-parsing tests for the glob/multi-target surface added to
`provision`, `ws destroy`, `ws diff`, `ws disconnect`, `ws update`, and `lint`.

Covers argument shape only (nargs, required-ness, removed/renamed flags) via
`click.testing.CliRunner` with no container wiring — these assertions run
purely against click's own argument parser and our `--help`/validation text,
so they hold regardless of whether a real workspace is present.
"""

from __future__ import annotations

from click.testing import CliRunner

from winter_cli.modules.lint.command import lint_command
from winter_cli.modules.provision.command import provision_command
from winter_cli.modules.workspace.command import ws_destroy, ws_diff, ws_disconnect, ws_update


class TestProvisionCli:
    def test_no_patterns_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(provision_command, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_stage_option_present_in_help(self) -> None:
        result = CliRunner().invoke(provision_command, ["--help"])
        assert result.exit_code == 0
        assert "--stage" in result.output

    def test_old_trailing_subtarget_positional_is_gone(self) -> None:
        """The old `SUBTARGET` positional metavar no longer appears in --help."""
        result = CliRunner().invoke(provision_command, ["--help"])
        assert result.exit_code == 0
        assert "SUBTARGET" not in result.output

    def test_empty_pattern_rejected(self) -> None:
        result = CliRunner().invoke(provision_command, [""])
        assert result.exit_code != 0
        assert "Empty pattern" in result.output

    def test_slash_qualified_pattern_rejected(self) -> None:
        result = CliRunner().invoke(provision_command, ["alpha/winter"])
        assert result.exit_code != 0
        assert "no '/'" in result.output


class TestWsDestroyCli:
    def test_no_patterns_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(ws_destroy, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_slash_qualified_pattern_rejected(self) -> None:
        result = CliRunner().invoke(ws_destroy, ["alpha/winter"])
        assert result.exit_code != 0
        assert "no '/'" in result.output

    def test_force_help_mentions_confirmation_bypass(self) -> None:
        result = CliRunner().invoke(ws_destroy, ["--help"])
        assert result.exit_code == 0
        assert "confirmation" in result.output.lower()


class TestWsDiffCli:
    def test_no_patterns_is_accepted_by_argument_parser(self) -> None:
        """PATTERNS is optional for diff — no click usage error for zero args
        (any failure past argument parsing comes from container resolution)."""
        result = CliRunner().invoke(ws_diff, [])
        assert "Missing argument" not in result.output
        assert "Usage:" not in result.output or result.exit_code != 2

    def test_repo_option_removed(self) -> None:
        result = CliRunner().invoke(ws_diff, ["alpha", "--repo", "winter"])
        assert result.exit_code != 0
        assert "no such option" in result.output.lower()

    def test_help_documents_pattern_grammar(self) -> None:
        result = CliRunner().invoke(ws_diff, ["--help"])
        assert result.exit_code == 0
        assert "PATTERNS" in result.output
        # No --repo entry in the options section (it's only mentioned prose-wise
        # as the flag PATTERNS replaces) — the options block itself has no
        # standalone "--repo TEXT" line.
        assert "--repo TEXT" not in result.output

    def test_invalid_pattern_two_slashes_rejected(self) -> None:
        result = CliRunner().invoke(ws_diff, ["alpha/winter/extra"])
        assert result.exit_code != 0
        assert "one '/' max" in result.output


class TestWsDisconnectCli:
    def test_no_patterns_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(ws_disconnect, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_invalid_pattern_two_slashes_rejected(self) -> None:
        result = CliRunner().invoke(ws_disconnect, ["alpha/winter/extra"])
        assert result.exit_code != 0
        assert "one '/' max" in result.output

    def test_help_documents_pattern_grammar(self) -> None:
        result = CliRunner().invoke(ws_disconnect, ["--help"])
        assert result.exit_code == 0
        assert "PATTERNS" in result.output


class TestWsUpdateCli:
    def test_no_repos_is_accepted_by_argument_parser(self) -> None:
        """REPOS is optional for update — no click usage error for zero args."""
        result = CliRunner().invoke(ws_update, [])
        assert "Missing argument" not in result.output

    def test_empty_pattern_rejected(self) -> None:
        result = CliRunner().invoke(ws_update, [""])
        assert result.exit_code != 0
        assert "Empty pattern" in result.output

    def test_slash_qualified_pattern_rejected(self) -> None:
        result = CliRunner().invoke(ws_update, ["alpha/winter"])
        assert result.exit_code != 0
        assert "no '/'" in result.output
        assert "environment" not in result.output.lower()

    def test_help_documents_repos_grammar(self) -> None:
        result = CliRunner().invoke(ws_update, ["--help"])
        assert result.exit_code == 0
        assert "REPOS" in result.output


class TestLintCli:
    def test_no_scopes_is_accepted_by_argument_parser(self) -> None:
        """SCOPES is optional for lint — no click usage error for zero args."""
        result = CliRunner().invoke(lint_command, [])
        assert "Missing argument" not in result.output

    def test_empty_pattern_rejected(self) -> None:
        result = CliRunner().invoke(lint_command, [""])
        assert result.exit_code != 0
        assert "Empty pattern" in result.output

    def test_slash_qualified_pattern_rejected(self) -> None:
        result = CliRunner().invoke(lint_command, ["alpha/winter"])
        assert result.exit_code != 0
        assert "no '/'" in result.output
        assert "environment" not in result.output.lower()

    def test_help_documents_scopes_grammar(self) -> None:
        result = CliRunner().invoke(lint_command, ["--help"])
        assert result.exit_code == 0
        assert "SCOPES" in result.output
