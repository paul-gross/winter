"""CLI argument-parsing tests for the glob/multi-target surface added to
`provision`, `clean`, `ws destroy`, `ws diff`, `ws disconnect`, `ws update`,
`ws reset`, `ws clean`, and `lint`.

Covers argument shape only (nargs, required-ness, removed/renamed flags) via
`click.testing.CliRunner` with no container wiring — these assertions run
purely against click's own argument parser and our `--help`/validation text,
so they hold regardless of whether a real workspace is present.
"""

from __future__ import annotations

from click.testing import CliRunner

from winter_cli.modules.lint.command import lint_command
from winter_cli.modules.provision.clean_command import clean_command
from winter_cli.modules.provision.command import provision_command
from winter_cli.modules.workspace.command import (
    ws_clean,
    ws_destroy,
    ws_diff,
    ws_disconnect,
    ws_reset,
    ws_update,
)


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


class TestCleanCli:
    def test_no_patterns_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(clean_command, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_stage_option_present_in_help(self) -> None:
        result = CliRunner().invoke(clean_command, ["--help"])
        assert result.exit_code == 0
        assert "--stage" in result.output

    def test_name_option_present_in_help(self) -> None:
        result = CliRunner().invoke(clean_command, ["--help"])
        assert result.exit_code == 0
        assert "--name" in result.output

    def test_empty_pattern_rejected(self) -> None:
        result = CliRunner().invoke(clean_command, [""])
        assert result.exit_code != 0
        assert "Empty pattern" in result.output

    def test_slash_qualified_pattern_rejected(self) -> None:
        result = CliRunner().invoke(clean_command, ["alpha/winter"])
        assert result.exit_code != 0
        assert "no '/'" in result.output

    def test_help_states_it_is_not_ws_clean(self) -> None:
        """This is a distinct verb from `winter ws clean` — its own help text
        must say so, so the two are never confused."""
        result = CliRunner().invoke(clean_command, ["--help"])
        assert result.exit_code == 0
        assert "ws clean" in result.output

    def test_no_reset_destroy_or_seed_flags(self) -> None:
        """`clean` always runs the `clean` action — there is no action flag to
        select, unlike `provision`."""
        result = CliRunner().invoke(clean_command, ["--help"])
        assert result.exit_code == 0
        assert "--reset" not in result.output
        assert "--destroy" not in result.output
        assert "--seed" not in result.output

    def test_no_force_flag(self) -> None:
        """`clean` is ungated: there is no `--force` to skip a prompt that
        does not exist."""
        result = CliRunner().invoke(clean_command, ["--help"])
        assert result.exit_code == 0
        assert "--force" not in result.output


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


class TestWsResetCli:
    def test_no_args_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(ws_reset, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_ref_alone_with_no_pattern_is_rejected(self) -> None:
        """REF is always trailing — a single arg is parsed as PATTERNS with no REF."""
        result = CliRunner().invoke(ws_reset, ["origin/main"])
        assert result.exit_code != 0
        assert "trailing REF" in result.output

    def test_invalid_pattern_two_slashes_rejected(self) -> None:
        result = CliRunner().invoke(ws_reset, ["alpha/winter/extra", "origin/main"])
        assert result.exit_code != 0
        assert "one '/' max" in result.output

    def test_soft_and_hard_are_mutually_exclusive(self) -> None:
        result = CliRunner().invoke(ws_reset, ["alpha", "origin/main", "--soft", "--hard"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_help_documents_pattern_and_ref_grammar(self) -> None:
        result = CliRunner().invoke(ws_reset, ["--help"])
        assert result.exit_code == 0
        assert "PATTERNS" in result.output
        assert "REF" in result.output
        assert "--soft" in result.output
        assert "--mixed" in result.output
        assert "--hard" in result.output
        assert "--dry-run" in result.output


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


class TestWsCleanCli:
    def test_no_patterns_is_a_usage_error(self) -> None:
        """There is deliberately no implicit "every worktree" default for a
        command that deletes irrecoverably."""
        result = CliRunner().invoke(ws_clean, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "PATTERNS" in result.output

    def test_invalid_pattern_two_slashes_rejected(self) -> None:
        result = CliRunner().invoke(ws_clean, ["alpha/winter/extra"])
        assert result.exit_code != 0
        assert "one '/' max" in result.output

    def test_json_without_force_or_dry_run_is_refused(self) -> None:
        """The confirmation prompt would corrupt the NDJSON stream and hang a
        non-interactive consumer, so the combination is refused rather than
        silently auto-forced into an unrecoverable delete."""
        result = CliRunner().invoke(ws_clean, ["alpha", "--json"])
        assert result.exit_code != 0
        assert "--json requires --force or --dry-run" in result.output

    def test_json_with_dry_run_is_accepted_by_the_parser(self) -> None:
        result = CliRunner().invoke(ws_clean, ["alpha", "--json", "--dry-run"])
        assert "--json requires" not in result.output

    def test_json_with_force_is_accepted_by_the_parser(self) -> None:
        result = CliRunner().invoke(ws_clean, ["alpha", "--json", "--force"])
        assert "--json requires" not in result.output

    def test_force_help_mentions_confirmation_bypass(self) -> None:
        result = CliRunner().invoke(ws_clean, ["--help"])
        assert result.exit_code == 0
        assert "--force" in result.output
        assert "confirmation" in result.output.lower()

    def test_help_documents_pattern_grammar_and_flags(self) -> None:
        result = CliRunner().invoke(ws_clean, ["--help"])
        assert result.exit_code == 0
        assert "PATTERNS" in result.output
        assert "--dry-run" in result.output
        assert "--json" in result.output

    def test_help_states_ignored_files_are_never_removed(self) -> None:
        """The never-`-x` invariant is the reason a clean cannot force a
        re-provision; it belongs where an agent reading `--help` will see it."""
        result = CliRunner().invoke(ws_clean, ["--help"])
        assert result.exit_code == 0
        assert "gnored" in result.output
