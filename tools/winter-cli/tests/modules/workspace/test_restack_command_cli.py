"""CLI argument-shape tests for `winter ws restack`.

Covers the command-body guards Decision 1 puts there rather than in
`EnvRestackPlanService`: the arity check (fewer than two positionals) and
the chain-element shape check (empty, glob-shaped, or `/`-qualified) — both
raise `click.ClickException` before `cli_ctx(ctx).container` is ever
touched, so these tests invoke the bare command with no `obj` at all,
mirroring `test_command_glob_cli.py`'s style for the same reason.

What a wrong implementation could still pass here: a check that only fires
on the *first* chain element, or one that validates the trailing BASE with
the same glob/slash rule as a chain element (BASE is an arbitrary ref, never
subject to that rule) — the per-position and BASE-exemption tests below
exist specifically to catch those.
"""

from __future__ import annotations

from click.testing import CliRunner

from winter_cli.modules.workspace.command import ws_restack


class TestArity:
    def test_zero_positionals_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(ws_restack, [])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "ENV" in result.output

    def test_one_positional_is_rejected_by_the_command_body(self) -> None:
        """A single positional parses fine (click's own `nargs=-1` accepts
        it), so the arity refusal must come from the command body's own
        `len(args) < 2` check, not from click's parser."""
        result = CliRunner().invoke(ws_restack, ["only-one"])
        assert result.exit_code != 0
        assert "at least one ENV" in result.output

    def test_two_positionals_pass_the_arity_check(self) -> None:
        """Exactly two positionals (one chain element + BASE) clears the
        arity guard — the run then fails later at container resolution
        (there is no `obj` here), which is a distinct, unrelated error from
        the "at least one ENV" usage message this test guards against."""
        result = CliRunner().invoke(ws_restack, ["env1", "master"])
        assert "at least one ENV" not in result.output


class TestChainElementShape:
    """Each check exercised on both the sole chain element (single-link
    chain) and a non-trailing element (multi-link chain), so a wrong
    implementation that only validates `chain[0]` cannot pass both."""

    def test_empty_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["", "master"])
        assert result.exit_code != 0
        assert "Empty chain element" in result.output

    def test_empty_non_trailing_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env2", "", "master"])
        assert result.exit_code != 0
        assert "Empty chain element" in result.output

    def test_glob_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env*", "master"])
        assert result.exit_code != 0
        assert "not globs" in result.output

    def test_glob_non_trailing_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env2", "env?", "master"])
        assert result.exit_code != 0
        assert "not globs" in result.output

    def test_bracket_glob_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env[12]", "master"])
        assert result.exit_code != 0
        assert "not globs" in result.output

    def test_slash_qualified_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["alpha/winter", "master"])
        assert result.exit_code != 0
        assert "no '<env>/<repo>' scoping" in result.output

    def test_slash_qualified_non_trailing_element_is_rejected(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env2", "alpha/winter", "master"])
        assert result.exit_code != 0
        assert "no '<env>/<repo>' scoping" in result.output

    def test_a_shape_error_on_one_element_is_reported_even_with_others_valid(self) -> None:
        """A three-element chain where only the middle element is malformed
        still refuses — pins that every element is checked, not just
        whichever one happens to be checked first and short-circuits."""
        result = CliRunner().invoke(ws_restack, ["env3", "bad*", "env1", "master"])
        assert result.exit_code != 0
        assert "not globs" in result.output


class TestBaseIsExemptFromChainElementRules:
    """BASE is the trailing positional, resolved as an arbitrary ref by the
    plan service — Decision 1 explicitly carves it out of the chain-element
    shape rule. A wrong implementation that applies `_validate_restack_element`
    to BASE too would fail these."""

    def test_glob_shaped_base_is_not_rejected_by_the_chain_element_rule(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env1", "origin/*"])
        assert "not globs" not in result.output

    def test_slash_qualified_base_is_not_rejected_by_the_chain_element_rule(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env1", "origin/main"])
        assert "no '<env>/<repo>' scoping" not in result.output

    def test_empty_base_is_still_rejected_by_its_own_check(self) -> None:
        result = CliRunner().invoke(ws_restack, ["env1", ""])
        assert result.exit_code != 0
        assert "Empty BASE" in result.output
