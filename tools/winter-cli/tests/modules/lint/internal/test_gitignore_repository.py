from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.lint.internal.gitignore_repository import (
    GitIgnoreRepository,
    _unquote_c_style,
)

SCOPE_ROOT = Path("/ws/repo")
_TOPLEVEL_CMD = "git rev-parse --show-toplevel"


def _runner(*, toplevel: SubprocessResult, check_ignore: dict[str, SubprocessResult] | None = None) -> FakeSubprocessRunner:
    responses = {_TOPLEVEL_CMD: toplevel}
    responses.update(check_ignore or {})
    return FakeSubprocessRunner(run_responses=responses)


def test_returns_candidates_unchanged_when_root_is_not_a_repo() -> None:
    """`rev-parse --show-toplevel` exiting non-zero (not a repo, or git can't be invoked) is an unfiltered fallback."""
    runner = _runner(toplevel=SubprocessResult(128, "", "fatal: not a git repository"))
    repo = GitIgnoreRepository(runner)
    candidates = [SCOPE_ROOT / "a.md", SCOPE_ROOT / "b.md"]

    assert repo.filter_ignored(SCOPE_ROOT, candidates) == candidates
    # No check-ignore call is made once ownership can't be established.
    assert all("check-ignore" not in cmd for cmd, _cwd in runner.run_calls)


def test_returns_candidates_unchanged_when_toplevel_is_an_enclosing_repo() -> None:
    """`scope_root` resolves to a repo, but not to *its own* toplevel — an enclosing repo owns it instead."""
    runner = _runner(toplevel=SubprocessResult(0, str(SCOPE_ROOT.parent) + "\n", ""))
    repo = GitIgnoreRepository(runner)
    candidates = [SCOPE_ROOT / "a.md"]

    assert repo.filter_ignored(SCOPE_ROOT, candidates) == candidates


def test_drops_matched_candidates_when_root_owns_the_repo() -> None:
    a = SCOPE_ROOT / "a.md"
    b = SCOPE_ROOT / "b.md"
    check_ignore_key = f"git -c core.quotePath=false check-ignore {a} {b}"
    runner = _runner(
        toplevel=SubprocessResult(0, str(SCOPE_ROOT) + "\n", ""),
        check_ignore={check_ignore_key: SubprocessResult(0, f"{a}\n", "")},
    )
    repo = GitIgnoreRepository(runner)

    assert repo.filter_ignored(SCOPE_ROOT, [a, b]) == [b]


def test_check_ignore_failure_fails_open_for_that_batch_only() -> None:
    """A batch whose `check-ignore` call itself errors (e.g. `ARG_MAX`, surfaced as -1) stays unfiltered — it
    does not take the rest of the candidates down with it."""
    a = SCOPE_ROOT / "a.md"
    b = SCOPE_ROOT / "b.md"
    check_ignore_key = f"git -c core.quotePath=false check-ignore {a} {b}"
    runner = _runner(
        toplevel=SubprocessResult(0, str(SCOPE_ROOT) + "\n", ""),
        check_ignore={check_ignore_key: SubprocessResult(-1, "", "Argument list too long")},
    )
    repo = GitIgnoreRepository(runner)

    assert repo.filter_ignored(SCOPE_ROOT, [a, b]) == [a, b]


def test_a_failed_batch_does_not_affect_a_neighboring_successful_batch() -> None:
    """Two batches, one erroring and one matching: only the erroring batch's candidates survive unfiltered."""
    failing_batch = [SCOPE_ROOT / f"f{i}.md" for i in range(500)]
    ignored = SCOPE_ROOT / "ignored.md"
    failing_key = "git -c core.quotePath=false check-ignore " + " ".join(str(c) for c in failing_batch)
    succeeding_key = f"git -c core.quotePath=false check-ignore {ignored}"
    runner = _runner(
        toplevel=SubprocessResult(0, str(SCOPE_ROOT) + "\n", ""),
        check_ignore={
            failing_key: SubprocessResult(-1, "", "Argument list too long"),
            succeeding_key: SubprocessResult(0, f"{ignored}\n", ""),
        },
    )
    repo = GitIgnoreRepository(runner)

    result = repo.filter_ignored(SCOPE_ROOT, [*failing_batch, ignored])

    assert result == failing_batch  # the failing batch stays; `ignored` alone was dropped by the healthy batch


def test_candidates_are_chunked_into_batches() -> None:
    """A tree with more candidates than one batch still costs a handful of `check-ignore` calls, not one per file."""
    candidates = [SCOPE_ROOT / f"f{i}.md" for i in range(1200)]
    runner = _runner(toplevel=SubprocessResult(0, str(SCOPE_ROOT) + "\n", ""))
    # FakeSubprocessRunner raises on an unregistered command — register every
    # batch's exact argv with "nothing ignored" so the call itself succeeds.
    for i in range(0, len(candidates), 500):
        batch = candidates[i : i + 500]
        key = "git -c core.quotePath=false check-ignore " + " ".join(str(c) for c in batch)
        runner._run_responses[key] = SubprocessResult(1, "", "")  # rc=1: no match in this batch

    repo = GitIgnoreRepository(runner)
    result = repo.filter_ignored(SCOPE_ROOT, candidates)

    assert result == candidates
    check_ignore_calls = [c for c in runner.run_calls if "check-ignore" in c[0]]
    assert len(check_ignore_calls) == 3, f"Expected 1200 candidates to batch into 3 calls; got {len(check_ignore_calls)}"


def test_quoted_line_with_non_ascii_path_is_unquoted_and_matched() -> None:
    cafe = SCOPE_ROOT / "café.md"
    quoted_output = '"' + str(cafe).replace("é", "\\303\\251") + '"\n'
    check_ignore_key = f"git -c core.quotePath=false check-ignore {cafe}"
    runner = _runner(
        toplevel=SubprocessResult(0, str(SCOPE_ROOT) + "\n", ""),
        check_ignore={check_ignore_key: SubprocessResult(0, quoted_output, "")},
    )
    repo = GitIgnoreRepository(runner)

    assert repo.filter_ignored(SCOPE_ROOT, [cafe]) == []


def test_unquote_c_style_decodes_octal_and_named_escapes() -> None:
    assert _unquote_c_style('".terraform/modules/caf\\303\\251/README.md"') == ".terraform/modules/café/README.md"
    assert _unquote_c_style('"qu\\"ote.md"') == 'qu"ote.md'


def test_unquote_c_style_returns_none_for_unparseable_input() -> None:
    assert _unquote_c_style("not-quoted-at-all") is None
    assert _unquote_c_style('"unterminated') is None
    assert _unquote_c_style('"trailing\\"') is None
