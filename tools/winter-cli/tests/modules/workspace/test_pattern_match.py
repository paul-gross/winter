from __future__ import annotations

import click
import pytest

from winter_cli.modules.workspace.pattern_match import (
    has_glob,
    is_single_literal_pattern,
    resolve_name_patterns,
    validate_bare_name_pattern,
    validate_env_pattern,
)

# ── is_single_literal_pattern ─────────────────────────────────────────────────


def test_single_literal_env_svc_returns_true() -> None:
    """Single literal <env>/<svc> with no metacharacters → True."""
    assert is_single_literal_pattern(["alpha/api"]) is True


def test_bare_env_no_slash_returns_false() -> None:
    """Bare <env> (no slash) → False; it expands to all services."""
    assert is_single_literal_pattern(["alpha"]) is False


def test_wildcard_star_returns_false() -> None:
    """Single pattern with a `*` metacharacter → False."""
    assert is_single_literal_pattern(["alpha/worker-*"]) is False


def test_wildcard_question_mark_returns_false() -> None:
    """Single pattern with a `?` metacharacter → False."""
    assert is_single_literal_pattern(["alpha/worker-?"]) is False


def test_wildcard_bracket_returns_false() -> None:
    """Single pattern with a `[` metacharacter → False."""
    assert is_single_literal_pattern(["alpha/worker-[ab]"]) is False


def test_two_patterns_returns_false() -> None:
    """Two patterns → False (multi-scope regardless of content)."""
    assert is_single_literal_pattern(["alpha/api", "beta/api"]) is False


def test_empty_list_returns_false() -> None:
    """Empty pattern list → False (no patterns = all services)."""
    assert is_single_literal_pattern([]) is False


def test_cross_env_wildcard_returns_false() -> None:
    """Cross-env pattern `*/backend` contains `*` → False."""
    assert is_single_literal_pattern(["*/backend"]) is False


# ── has_glob ──────────────────────────────────────────────────────────────────


def test_has_glob_literal_returns_false() -> None:
    assert has_glob("alpha") is False


def test_has_glob_star_returns_true() -> None:
    assert has_glob("feature-*") is True


def test_has_glob_question_mark_returns_true() -> None:
    assert has_glob("alpha?") is True


def test_has_glob_bracket_returns_true() -> None:
    assert has_glob("alpha[ab]") is True


# ── validate_env_pattern ──────────────────────────────────────────────────────


def test_validate_env_pattern_accepts_literal() -> None:
    validate_env_pattern("alpha")  # no raise


def test_validate_env_pattern_accepts_glob() -> None:
    validate_env_pattern("feature-*")  # no raise


def test_validate_env_pattern_rejects_empty() -> None:
    with pytest.raises(click.ClickException, match="Empty pattern"):
        validate_env_pattern("")


def test_validate_env_pattern_rejects_slash() -> None:
    with pytest.raises(click.ClickException, match="no '/'"):
        validate_env_pattern("alpha/winter")


# ── validate_bare_name_pattern ────────────────────────────────────────────────


def test_validate_bare_name_pattern_accepts_literal() -> None:
    validate_bare_name_pattern("winter")  # no raise


def test_validate_bare_name_pattern_accepts_glob() -> None:
    validate_bare_name_pattern("winter-*")  # no raise


def test_validate_bare_name_pattern_rejects_empty() -> None:
    with pytest.raises(click.ClickException, match="Empty pattern"):
        validate_bare_name_pattern("")


def test_validate_bare_name_pattern_rejects_slash() -> None:
    with pytest.raises(click.ClickException, match="no '/'"):
        validate_bare_name_pattern("alpha/winter")


def test_validate_bare_name_pattern_message_does_not_say_environments() -> None:
    """Unlike `validate_env_pattern`, the message must not claim the target is an environment."""
    with pytest.raises(click.ClickException) as excinfo:
        validate_bare_name_pattern("alpha/winter")
    assert "environment" not in str(excinfo.value).lower()


# ── resolve_name_patterns ─────────────────────────────────────────────────────


def test_resolve_name_patterns_literal_only_skips_discovery() -> None:
    def discover_names() -> list[str]:
        raise AssertionError("discovery should not run for a pure-literal pattern list")

    assert resolve_name_patterns(["beta", "alpha"], discover_names) == ["alpha", "beta"]


def test_resolve_name_patterns_literal_verbatim_even_if_undiscoverable() -> None:
    assert resolve_name_patterns(["typo"], lambda: ["alpha", "beta"]) == ["typo"]


def test_resolve_name_patterns_glob_expands_against_discovered() -> None:
    assert resolve_name_patterns(["*"], lambda: ["gamma", "alpha", "beta"]) == ["alpha", "beta", "gamma"]


def test_resolve_name_patterns_glob_matching_none_returns_empty() -> None:
    assert resolve_name_patterns(["zzz-*"], lambda: ["alpha", "beta"]) == []


def test_resolve_name_patterns_dedupes_literal_and_glob_overlap() -> None:
    assert resolve_name_patterns(["alpha", "*"], lambda: ["alpha", "beta"]) == ["alpha", "beta"]
