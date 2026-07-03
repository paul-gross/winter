from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable

import click


def has_glob(pattern: str) -> bool:
    """Whether `pattern` (or one segment of it) contains an fnmatch wildcard (`*`, `?`, `[`)."""
    return any(c in pattern for c in "*?[")


def validate_env_pattern(pattern: str) -> None:
    """Validate a bare env-level pattern (`provision`, `ws destroy`): non-empty, no `/`.

    Env-level operations (`winter provision`, `winter ws destroy`) select whole
    feature environments, not `<env>/<repo>` worktrees — unlike `<env>/<repo>`
    commands (`fetch`/`pull`/`push`/`status`/`diff`/…), a pattern here is a bare
    glob over env names only. Reject a `/`-qualified pattern up front with a
    clear error rather than silently matching nothing.
    """
    if not pattern:
        raise click.ClickException("Empty pattern is not allowed")
    if "/" in pattern:
        raise click.ClickException(
            f"Invalid pattern '{pattern}' — env-level patterns select whole environments, not '<env>/<repo>' (no '/')"
        )


def validate_bare_name_pattern(pattern: str) -> None:
    """Validate a bare name-level pattern (`ws update`, `lint`): non-empty, no `/`.

    These commands select flat names — standalone-repo names for `ws update`,
    project-repo-or-env names for `lint` — not `<env>/<repo>` worktrees, so a
    pattern here is a bare glob over a single name segment. Reject a
    `/`-qualified pattern up front with a clear error rather than silently
    matching nothing.
    """
    if not pattern:
        raise click.ClickException("Empty pattern is not allowed")
    if "/" in pattern:
        raise click.ClickException(
            f"Invalid pattern '{pattern}' — this command takes bare names, not '<segment>/<segment>' (no '/')"
        )


def resolve_name_patterns(patterns: Iterable[str], discover_names: Callable[[], Iterable[str]]) -> list[str]:
    """Resolve bare-name PATTERNS to concrete names, in deterministic order.

    Shared by every command that selects targets via a flat literal-or-glob
    name list (env names for `provision`/`ws destroy`, repo-or-env names for
    `lint`). A literal pattern (no glob metacharacter) is always included
    verbatim, even when it matches nothing discoverable — callers surface
    their own "not found" error for an unmatched literal rather than
    silently dropping it. A glob pattern is expanded against
    `discover_names()` (called at most once, and only when at least one
    PATTERN is a glob), deduped against the literal set and sorted, so a
    mixed invocation never resolves the same name twice.
    """
    patterns = list(patterns)
    literal = {p for p in patterns if not has_glob(p)}
    names: list[str] = sorted(literal)
    if any(has_glob(p) for p in patterns):
        names.extend(
            sorted(name for name in discover_names() if name not in literal and matches_any_pattern(name, "", patterns))
        )
    return names


def is_single_literal_pattern(patterns: Iterable[str]) -> bool:
    """Return True only when there is exactly one pattern and it is a literal <env>/<svc>.

    A literal pattern contains a `/` separator and has no glob metacharacters
    (`*`, `?`, `[`). This is the only case where multi-scope output prefixing
    should be suppressed — any other selection (bare env, wildcard, cross-env,
    multiple patterns, or no patterns) may match multiple services.
    """
    seq = list(patterns)
    if len(seq) != 1:
        return False
    p = seq[0]
    return "/" in p and not any(c in p for c in "*?[")


def matches_pattern(env_name: str, repo_name: str, pattern: str) -> bool:
    """Match `<env>/<repo>` against a segment-aware glob.

    Bare patterns (no '/') are treated as `<pattern>/*`. Each segment uses
    fnmatch — `*` matches anything within a segment, `?` matches one char.
    `*` does not cross `/`, so `*/winter` matches every env's winter worktree
    but not `alpha/winter-product`.
    """
    if "/" not in pattern:
        pattern = f"{pattern}/*"
    env_pat, repo_pat = pattern.split("/", 1)
    return fnmatch.fnmatchcase(env_name, env_pat) and fnmatch.fnmatchcase(repo_name, repo_pat)


def matches_any_pattern(env_name: str, repo_name: str, patterns: Iterable[str]) -> bool:
    return any(matches_pattern(env_name, repo_name, p) for p in patterns)
