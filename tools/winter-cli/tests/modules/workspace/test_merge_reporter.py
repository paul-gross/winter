"""Tests for `merge_reporter.{Stream,Json}MergeReporter`.

The NDJSON shape (`type`, `scope`, `repo`, `result`, `ahead`, `behind`)
is what external automation depends on — pin it here so silent drift
breaks the suite.
"""

from __future__ import annotations

import json

from winter_cli.modules.workspace.merge_reporter import (
    IMergeReporter,
    JsonMergeReporter,
    StreamMergeReporter,
)
from winter_cli.modules.workspace.models import MergeResult


class _CapturingClick:
    """Minimal click stand-in — records every echo for inspection."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, bool]] = []

    def echo(self, message: str, err: bool = False) -> None:
        self.lines.append((message, err))


def _conforms_stream_reporter(x: StreamMergeReporter) -> IMergeReporter:
    return x


def _conforms_json_reporter(x: JsonMergeReporter) -> IMergeReporter:
    return x


# --- JSON reporter ------------------------------------------------------------


def test_json_merge_started_emits_source_ref() -> None:
    """`merge_started` carries the source ref so consumers can correlate events."""
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.merge_started("origin/master")

    assert click.lines == [(json.dumps({"type": "merge_started", "source_ref": "origin/master"}), False)]


def test_json_merge_completed_emits_success_flag() -> None:
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.merge_completed(True)
    reporter.merge_completed(False)

    payloads = [json.loads(line) for line, _ in click.lines]
    assert payloads == [
        {"type": "merge_completed", "success": True},
        {"type": "merge_completed", "success": False},
    ]


def test_json_repo_merged_full_envelope_for_fast_forward() -> None:
    """Lock the exact NDJSON envelope: type, scope, repo, result, ahead, behind."""
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.repo_merged("gamma", "demo", MergeResult.fast_forwarded, 0, 0)

    payload = json.loads(click.lines[0][0])
    assert payload == {
        "type": "repo_merged",
        "scope": "gamma",
        "repo": "demo",
        "result": "fast_forwarded",
        "ahead": 0,
        "behind": 0,
    }


def test_json_repo_merged_diverged_carries_ahead_behind() -> None:
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.repo_merged("alpha", "winter", MergeResult.diverged, 3, 2)

    payload = json.loads(click.lines[0][0])
    assert payload["result"] == "diverged"
    assert payload["ahead"] == 3
    assert payload["behind"] == 2


def test_json_repo_merged_uses_standalone_scope_label() -> None:
    """Standalones use scope_label='standalone' to mirror pull/fetch conventions."""
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.repo_merged("standalone", "winter-codeberg", MergeResult.up_to_date, 0, 0)

    payload = json.loads(click.lines[0][0])
    assert payload["scope"] == "standalone"
    assert payload["repo"] == "winter-codeberg"


def test_json_repo_merged_skipped_missing_ref_emits_kebab_result() -> None:
    """`MergeResult.skipped_missing_ref` serializes to the kebab-case enum value."""
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.repo_merged("gamma", "winter", MergeResult.skipped_missing_ref, 0, 0)

    payload = json.loads(click.lines[0][0])
    assert payload["result"] == "skipped-missing-ref"


def test_json_reporter_emits_only_to_stdout_not_stderr() -> None:
    """NDJSON is structured machine output — every line on stdout, never stderr."""
    click = _CapturingClick()
    reporter = JsonMergeReporter(click)

    reporter.merge_started("alpha")
    reporter.repo_merged("gamma", "demo", MergeResult.diverged, 1, 1)
    reporter.repo_merged("gamma", "other", MergeResult.skipped_missing_ref, 0, 0)
    reporter.merge_completed(False)

    assert all(err is False for _line, err in click.lines)


# --- Stream reporter ----------------------------------------------------------


def test_stream_merge_started_announces_source_ref() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.merge_started("origin/master")

    assert click.lines == [("→ merging origin/master", False)]


def test_stream_merge_completed_success_uses_stdout() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.merge_completed(True)

    assert click.lines == [("\n✓ merge complete", False)]


def test_stream_merge_completed_failure_uses_stderr() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.merge_completed(False)

    assert click.lines == [("\n✗ merge had errors", True)]


def test_stream_repo_merged_diverged_to_stderr_with_counts() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.repo_merged("gamma", "demo", MergeResult.diverged, 3, 2)

    assert click.lines == [("[gamma/demo] diverged: +3/-2", True)]


def test_stream_repo_merged_skipped_to_stderr() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.repo_merged("gamma", "demo", MergeResult.skipped_missing_ref, 0, 0)

    assert click.lines == [("[gamma/demo] skipped: source ref not found", True)]


def test_stream_repo_merged_clean_outcomes_to_stdout() -> None:
    click = _CapturingClick()
    reporter = StreamMergeReporter(click)

    reporter.repo_merged("gamma", "a", MergeResult.merged, 0, 0)
    reporter.repo_merged("gamma", "b", MergeResult.fast_forwarded, 0, 0)
    reporter.repo_merged("gamma", "c", MergeResult.up_to_date, 0, 0)

    assert click.lines == [
        ("[gamma/a] merged (merge commit created)", False),
        ("[gamma/b] fast-forwarded", False),
        ("[gamma/c] up-to-date", False),
    ]
