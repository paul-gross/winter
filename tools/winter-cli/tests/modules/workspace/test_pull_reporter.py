"""Tests for `pull_reporter.{Stream,Json}PullReporter`.

`ws pull` now reports the number of commits integrated on a *successful*
fast-forward / merge / rebase (not only the ahead/behind span on
divergence). Pin both the human form (`fast-forwarded (+N)`, `merged (+N)`,
`rebased (+N)`, `up to date`) and the NDJSON envelope.
"""

from __future__ import annotations

import json

from winter_cli.modules.workspace.models import SyncResult
from winter_cli.modules.workspace.pull_reporter import (
    IPullReporter,
    JsonPullReporter,
    StreamPullReporter,
)


class _CapturingClick:
    """Minimal click stand-in — records every echo for inspection."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, bool]] = []

    def echo(self, message: str, err: bool = False) -> None:
        self.lines.append((message, err))


def _conforms_stream_reporter(x: StreamPullReporter) -> IPullReporter:
    return x


def _conforms_json_reporter(x: JsonPullReporter) -> IPullReporter:
    return x


# --- stream reporter ----------------------------------------------------------


def test_stream_repo_synced_integrate_outcomes_carry_commit_count() -> None:
    """ff / merge / rebase each report how many commits they brought in."""
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("alpha", "demo", SyncResult.fast_forwarded, commits=3, ahead=0, behind=0)
    reporter.repo_synced("alpha", "demo", SyncResult.merged, commits=2, ahead=0, behind=0)
    reporter.repo_synced("alpha", "demo", SyncResult.rebased, commits=4, ahead=0, behind=0)

    assert click.lines == [
        ("[alpha/demo] fast-forwarded (+3)", False),
        ("[alpha/demo] merged (+2)", False),
        ("[alpha/demo] rebased (+4)", False),
    ]


def test_stream_repo_synced_up_to_date_has_no_count() -> None:
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("alpha", "demo", SyncResult.up_to_date, commits=0, ahead=0, behind=0)

    assert click.lines == [("[alpha/demo] up to date", False)]


def test_stream_repo_synced_diverged_to_stderr_with_span() -> None:
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("alpha", "demo", SyncResult.diverged, commits=0, ahead=3, behind=2)

    assert click.lines == [("[alpha/demo] diverged: +3/-2", True)]


def test_stream_repo_synced_no_upstream_worktree_is_benign_on_stdout() -> None:
    """An unconnected feature worktree is a benign skip, not an error.

    Pulling a whole env where only one repo tracks a feature branch must not
    spam stderr with a line per unconnected worktree — the env still succeeds.
    """
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("alpha", "demo", SyncResult.no_upstream, commits=0, ahead=0, behind=0)

    assert click.lines == [("[alpha/demo] no upstream", False)]


def test_stream_repo_synced_no_upstream_standalone_stays_on_stderr() -> None:
    """A standalone repo is expected to track something — no upstream is a real
    failure there (it fails PullReport.success), so it stays on stderr."""
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.no_upstream, commits=0, ahead=0, behind=0)

    assert click.lines == [("[standalone/my-lib] no upstream", True)]


# --- json reporter ------------------------------------------------------------


def test_json_repo_synced_full_envelope_carries_commits() -> None:
    """Lock the NDJSON envelope: type, scope, repo, result, commits, ahead, behind."""
    click = _CapturingClick()
    reporter = JsonPullReporter(click)

    reporter.repo_synced("alpha", "demo", SyncResult.fast_forwarded, commits=3, ahead=0, behind=0)

    payload = json.loads(click.lines[0][0])
    assert payload == {
        "type": "repo_synced",
        "scope": "alpha",
        "repo": "demo",
        "result": "fast_forwarded",
        "commits": 3,
        "ahead": 0,
        "behind": 0,
        "pin_ref": "",
    }


def test_stream_repo_synced_held_pin_shows_ref() -> None:
    """held_pin reports the ref that was held (e.g. a tag name)."""
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.held_pin, commits=0, ahead=0, behind=0, pin_ref="v1.4.2")

    assert click.lines == [("[standalone/my-lib] held @ v1.4.2", False)]


def test_stream_repo_synced_re_pinned_shows_short_sha() -> None:
    """re_pinned reports the short SHA the branch pin advanced to."""
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.re_pinned, commits=0, ahead=0, behind=0, pin_ref="abc12345")

    assert click.lines == [("[standalone/my-lib] re-pinned → abc12345", False)]


def test_json_repo_synced_held_pin_envelope() -> None:
    """JSON envelope for held_pin carries pin_ref and result=held_pin."""
    click = _CapturingClick()
    reporter = JsonPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.held_pin, commits=0, ahead=0, behind=0, pin_ref="v1.4.2")

    payload = json.loads(click.lines[0][0])
    assert payload == {
        "type": "repo_synced",
        "scope": "standalone",
        "repo": "my-lib",
        "result": "held_pin",
        "commits": 0,
        "ahead": 0,
        "behind": 0,
        "pin_ref": "v1.4.2",
    }


def test_json_repo_synced_re_pinned_envelope() -> None:
    """JSON envelope for re_pinned carries pin_ref with the new short SHA."""
    click = _CapturingClick()
    reporter = JsonPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.re_pinned, commits=0, ahead=0, behind=0, pin_ref="abc12345")

    payload = json.loads(click.lines[0][0])
    assert payload == {
        "type": "repo_synced",
        "scope": "standalone",
        "repo": "my-lib",
        "result": "re_pinned",
        "commits": 0,
        "ahead": 0,
        "behind": 0,
        "pin_ref": "abc12345",
    }


# --- ws update --freeze outcomes ---------------------------------------------


def test_stream_repo_synced_pinned_shows_short_sha() -> None:
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced("standalone", "my-lib", SyncResult.pinned, commits=0, ahead=0, behind=0, pin_ref="abc12345")

    assert click.lines == [("[standalone/my-lib] pinned → abc12345", False)]


def test_stream_repo_synced_already_pinned_shows_the_existing_ref() -> None:
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced(
        "standalone", "my-lib", SyncResult.already_pinned, commits=0, ahead=0, behind=0, pin_ref="v1.4.2"
    )

    assert click.lines == [("[standalone/my-lib] already pinned @ v1.4.2", False)]


def test_stream_repo_synced_refused_goes_to_stderr_with_the_reason() -> None:
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.repo_synced(
        "standalone",
        "my-lib",
        SyncResult.refused,
        commits=0,
        ahead=0,
        behind=0,
        pin_ref="'my-lib' has uncommitted changes",
    )

    assert click.lines == [("[standalone/my-lib] refused: 'my-lib' has uncommitted changes", True)]


def test_json_repo_synced_freeze_outcomes_carry_result_and_pin_ref() -> None:
    click = _CapturingClick()
    reporter = JsonPullReporter(click)

    reporter.repo_synced("standalone", "a", SyncResult.pinned, commits=0, ahead=0, behind=0, pin_ref="abc12345")
    reporter.repo_synced("standalone", "b", SyncResult.already_pinned, commits=0, ahead=0, behind=0, pin_ref="v1.0")
    reporter.repo_synced("standalone", "c", SyncResult.refused, commits=0, ahead=0, behind=0, pin_ref="not cloned")

    payloads = [json.loads(line) for line, _err in click.lines]
    assert [(p["repo"], p["result"], p["pin_ref"]) for p in payloads] == [
        ("a", "pinned", "abc12345"),
        ("b", "already_pinned", "v1.0"),
        ("c", "refused", "not cloned"),
    ]
    assert all(p["type"] == "repo_synced" and p["scope"] == "standalone" for p in payloads)


_OFF_MAIN = "source checkout is on a detached HEAD, not main_branch 'main'; fetched origin but did not fast-forward"


def test_stream_source_checkout_warning_to_stderr() -> None:
    """A skipped source-checkout fast-forward surfaces as a per-repo warning on stderr."""
    click = _CapturingClick()
    reporter = StreamPullReporter(click)

    reporter.source_checkout_warning("demo", _OFF_MAIN)

    assert click.lines == [(f"[project/demo] warning: {_OFF_MAIN}", True)]


def test_json_source_checkout_warning_envelope() -> None:
    """Lock the NDJSON envelope for the source-checkout warning event."""
    click = _CapturingClick()
    reporter = JsonPullReporter(click)

    reporter.source_checkout_warning("demo", _OFF_MAIN)

    assert json.loads(click.lines[0][0]) == {
        "type": "source_checkout_warning",
        "repo": "demo",
        "warning": _OFF_MAIN,
    }
