from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.modules.lint.handler import LintHandler, LintParams
from winter_cli.modules.lint.models import (
    LintFinding,
    LintScope,
    LintScopeKind,
    LintScopeRequest,
    LintSummary,
)


class _FakeResolver:
    def __init__(self, scopes: list[LintScope]) -> None:
        self._scopes = scopes

    def resolve(self, request: LintScopeRequest) -> list[LintScope]:
        return self._scopes


class _FakeLintService:
    """Returns `summaries[i]` for the i-th `run()` call, cycling the last entry if exhausted.

    Mirrors the real `LintService.run`'s reporter contract — calls
    `reporter.started(scope)` then `reporter.finished(summary)` — so
    handler-level assertions about reporter events stay meaningful.
    """

    def __init__(self, summaries: list[LintSummary]) -> None:
        self._summaries = summaries
        self.run_calls: list[LintScope] = []

    def run(self, scope: LintScope, reporter: Any) -> LintSummary:
        idx = min(len(self.run_calls), len(self._summaries) - 1)
        self.run_calls.append(scope)
        reporter.started(scope)
        summary = self._summaries[idx]
        reporter.finished(summary)
        return summary


class _RecordingReporter:
    def __init__(self) -> None:
        self.started_scopes: list[LintScope] = []
        self.findings: list[LintFinding] = []
        self.summaries: list[LintSummary] = []

    def started(self, scope: LintScope) -> None:
        self.started_scopes.append(scope)

    def finding(self, finding: LintFinding) -> None:
        self.findings.append(finding)

    def finished(self, summary: LintSummary) -> None:
        self.summaries.append(summary)


def _make(
    scopes: list[LintScope],
    *,
    service_summaries: list[LintSummary] | None = None,
) -> tuple[LintHandler, _FakeLintService, _RecordingReporter, _RecordingReporter]:
    svc_summaries = service_summaries or [LintSummary(contributors=0, total=0, fails=0, warns=0)]
    svc = _FakeLintService(svc_summaries)
    stream_reporter = _RecordingReporter()
    json_reporter = _RecordingReporter()
    handler = LintHandler(
        lint_service=svc,  # type: ignore[arg-type]
        scope_resolver=_FakeResolver(scopes),  # type: ignore[arg-type]
        stream_reporter=stream_reporter,  # type: ignore[arg-type]
        json_reporter=json_reporter,  # type: ignore[arg-type]
    )
    return handler, svc, stream_reporter, json_reporter


def test_empty_changed_scope_short_circuits_without_running_lint_service() -> None:
    """A --changed run with no changed files short-circuits: reports clean, never calls LintService."""
    empty_changed = LintScope(kind=LintScopeKind.changed, label="changed (repo)", paths=[])
    handler, svc, stream_reporter, _ = _make([empty_changed])

    handler.run(LintParams(scope=LintScopeRequest(changed=True), output_json=False))

    # LintService.run must NOT be called — scripts never see WINTER_LINT_PATHS="".
    assert svc.run_calls == []
    # Reporter sees the scope header and a zero-contributor clean summary.
    assert len(stream_reporter.started_scopes) == 1
    assert stream_reporter.started_scopes[0] is empty_changed
    assert len(stream_reporter.summaries) == 1
    summary = stream_reporter.summaries[0]
    assert summary.contributors == 0
    assert summary.fails == 0
    assert summary.exit_code == 0


def test_non_empty_changed_scope_runs_lint_service() -> None:
    """A --changed run with at least one path delegates to LintService normally."""
    changed = LintScope(kind=LintScopeKind.changed, label="changed (repo)", paths=[Path("/ws/repo/src/foo.py")])
    handler, svc, _reporter, _ = _make([changed])

    handler.run(LintParams(scope=LintScopeRequest(changed=True), output_json=False))

    assert len(svc.run_calls) == 1


def test_all_scope_always_runs_lint_service() -> None:
    """A non-changed scope (--all) always delegates to LintService regardless of paths."""
    all_scope = LintScope(kind=LintScopeKind.all, label="all", paths=[])
    handler, svc, _, _ = _make([all_scope])

    handler.run(LintParams(scope=LintScopeRequest(all=True), output_json=False))

    assert len(svc.run_calls) == 1


def test_no_scopes_matched_reports_and_no_ops(capsys: pytest.CaptureFixture[str]) -> None:
    """A glob that resolved to zero scopes prints a message and never touches LintService."""
    handler, svc, _, _ = _make([])

    handler.run(LintParams(scope=LintScopeRequest(names=["zzz-*"]), output_json=False))

    assert svc.run_calls == []
    assert "No scope matched" in capsys.readouterr().out


def test_multiple_scopes_each_run_and_report_separately() -> None:
    """Multiple resolved scopes (multi-target/glob) each get their own service run + reporter events."""
    repo_a = LintScope(kind=LintScopeKind.repo, label="repo: a", paths=[Path("/ws/projects/a")])
    repo_b = LintScope(kind=LintScopeKind.repo, label="repo: b", paths=[Path("/ws/projects/b")])
    handler, svc, stream_reporter, _ = _make([repo_a, repo_b])

    handler.run(LintParams(scope=LintScopeRequest(names=["a", "b"]), output_json=False))

    assert svc.run_calls == [repo_a, repo_b]
    assert stream_reporter.started_scopes == [repo_a, repo_b]
    assert len(stream_reporter.summaries) == 2


def test_multiple_scopes_any_failure_exits_nonzero() -> None:
    """If any one of several resolved scopes fails, the whole run exits non-zero."""
    repo_a = LintScope(kind=LintScopeKind.repo, label="repo: a", paths=[Path("/ws/projects/a")])
    repo_b = LintScope(kind=LintScopeKind.repo, label="repo: b", paths=[Path("/ws/projects/b")])
    ok_summary = LintSummary(contributors=1, total=1, fails=0, warns=0)
    failing_summary = LintSummary(contributors=1, total=1, fails=1, warns=0)
    handler, svc, _, _ = _make([repo_a, repo_b], service_summaries=[ok_summary, failing_summary])

    with pytest.raises(SystemExit) as excinfo:
        handler.run(LintParams(scope=LintScopeRequest(names=["a", "b"]), output_json=False))

    assert excinfo.value.code != 0
    # Both scopes still ran despite the first one's success.
    assert svc.run_calls == [repo_a, repo_b]
