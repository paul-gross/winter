"""Tests for `fetch_reporter.{Stream,Json}FetchReporter`.

The per-repo `commits` count (how far a source checkout's local main was
fast-forwarded) is the signal `ws fetch` gained — pin both the human form
(`ok (+N)` vs `up to date`) and the NDJSON envelope so silent drift breaks
the suite.
"""

from __future__ import annotations

import json

from winter_cli.modules.workspace.fetch_reporter import (
    IFetchReporter,
    JsonFetchReporter,
    StreamFetchReporter,
)


class _CapturingClick:
    """Minimal click stand-in — records every echo for inspection."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, bool]] = []

    def echo(self, message: str, err: bool = False) -> None:
        self.lines.append((message, err))


def _conforms_stream_reporter(x: StreamFetchReporter) -> IFetchReporter:
    return x


def _conforms_json_reporter(x: JsonFetchReporter) -> IFetchReporter:
    return x


# --- stream reporter ----------------------------------------------------------


def test_stream_repo_fetched_advanced_reports_commit_count() -> None:
    """A source checkout that fast-forwarded reports `ok (+N)` on stdout."""
    click = _CapturingClick()
    reporter = StreamFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=True, commits=3, error=None)

    assert click.lines == [("[project/demo] ok (+3)", False)]


def test_stream_repo_fetched_up_to_date_when_no_commits() -> None:
    """Zero commits ⇒ `up to date`, distinguishing it from an advance."""
    click = _CapturingClick()
    reporter = StreamFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=True, commits=0, error=None)

    assert click.lines == [("[project/demo] up to date", False)]


def test_stream_repo_fetched_failure_to_stderr() -> None:
    click = _CapturingClick()
    reporter = StreamFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=False, commits=0, error="boom")

    assert click.lines == [("[project/demo] error: boom", True)]


# --- json reporter ------------------------------------------------------------


def test_json_repo_fetched_full_envelope_carries_commits() -> None:
    """Lock the NDJSON envelope: type, scope, repo, success, commits, error, warning."""
    click = _CapturingClick()
    reporter = JsonFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=True, commits=5, error=None)

    payload = json.loads(click.lines[0][0])
    assert payload == {
        "type": "repo_fetched",
        "scope": "project",
        "repo": "demo",
        "success": True,
        "commits": 5,
        "error": None,
        "warning": None,
    }


_OFF_MAIN = "source checkout is on branch 'master', not main_branch 'main'; fetched origin but did not fast-forward"


def test_stream_repo_fetched_off_main_warns_on_stderr() -> None:
    """A source checkout off main_branch reports the warning (naming both branches) on stderr."""
    click = _CapturingClick()
    reporter = StreamFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=True, commits=0, error=None, warning=_OFF_MAIN)

    assert click.lines == [(f"[project/demo] warning: {_OFF_MAIN}", True)]


def test_json_repo_fetched_off_main_carries_warning() -> None:
    """The NDJSON event carries the off-main warning and still reports success."""
    click = _CapturingClick()
    reporter = JsonFetchReporter(click)

    reporter.repo_fetched("project", "demo", success=True, commits=0, error=None, warning=_OFF_MAIN)

    payload = json.loads(click.lines[0][0])
    assert payload["success"] is True
    assert payload["warning"] == _OFF_MAIN
