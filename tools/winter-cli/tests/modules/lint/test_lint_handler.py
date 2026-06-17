from __future__ import annotations

from pathlib import Path

from winter_cli.modules.lint.handler import LintHandler, LintParams
from winter_cli.modules.lint.models import (
    LintFinding,
    LintScope,
    LintScopeKind,
    LintScopeRequest,
    LintSummary,
)


class _FakeResolver:
    def __init__(self, scope: LintScope) -> None:
        self._scope = scope

    def resolve(self, request: LintScopeRequest) -> LintScope:
        return self._scope


class _FakeLintService:
    def __init__(self, summary: LintSummary) -> None:
        self._summary = summary
        self.run_calls: list[LintScope] = []

    def run(self, scope: LintScope, reporter: object) -> LintSummary:
        self.run_calls.append(scope)
        return self._summary


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
    scope: LintScope,
    *,
    service_summary: LintSummary | None = None,
) -> tuple[LintHandler, _FakeLintService, _RecordingReporter, _RecordingReporter]:
    svc_summary = service_summary or LintSummary(contributors=0, total=0, fails=0, warns=0)
    svc = _FakeLintService(svc_summary)
    stream_reporter = _RecordingReporter()
    json_reporter = _RecordingReporter()
    handler = LintHandler(
        lint_service=svc,  # type: ignore[arg-type]
        scope_resolver=_FakeResolver(scope),  # type: ignore[arg-type]
        stream_reporter=stream_reporter,  # type: ignore[arg-type]
        json_reporter=json_reporter,  # type: ignore[arg-type]
    )
    return handler, svc, stream_reporter, json_reporter


def test_empty_changed_scope_short_circuits_without_running_lint_service() -> None:
    """A --changed run with no changed files short-circuits: reports clean, never calls LintService."""
    empty_changed = LintScope(kind=LintScopeKind.changed, label="changed (repo)", paths=[])
    handler, svc, stream_reporter, _ = _make(empty_changed)

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
    handler, svc, _reporter, _ = _make(changed)

    handler.run(LintParams(scope=LintScopeRequest(changed=True), output_json=False))

    assert len(svc.run_calls) == 1


def test_all_scope_always_runs_lint_service() -> None:
    """A non-changed scope (--all) always delegates to LintService regardless of paths."""
    all_scope = LintScope(kind=LintScopeKind.all, label="all", paths=[])
    handler, svc, _, _ = _make(all_scope)

    handler.run(LintParams(scope=LintScopeRequest(all=True), output_json=False))

    assert len(svc.run_calls) == 1
