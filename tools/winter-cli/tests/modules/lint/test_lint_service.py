from __future__ import annotations

from typing import Any

from winter_cli.modules.lint.lint_service import LintService
from winter_cli.modules.lint.models import (
    LintCheckOutcome,
    LintFinding,
    LintScope,
    LintScopeKind,
    LintStatus,
    LintSummary,
)

SCOPE = LintScope(kind=LintScopeKind.all, label="all", paths=[])


def _finding(source: str, status: LintStatus, check: str = "c") -> LintFinding:
    return LintFinding(source=source, check=check, status=status)


class _FakeCoreLint:
    """A core lint service fake: `run(scope) -> list[LintCheckOutcome]`."""

    def __init__(self, outcomes: list[LintCheckOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[LintScope] = []

    def run(self, scope: LintScope) -> list[LintCheckOutcome]:
        self.calls.append(scope)
        return list(self._outcomes)


class _FakeScalarLint:
    """A workspace-style lint service: `run(scope) -> outcome | None`."""

    def __init__(self, outcome: LintCheckOutcome | None) -> None:
        self._outcome = outcome
        self.calls: list[LintScope] = []

    def run(self, scope: LintScope) -> LintCheckOutcome | None:
        self.calls.append(scope)
        return self._outcome


class _FakeExtensionLint:
    def __init__(self, outcomes: list[LintCheckOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[Any] = []

    def run(self, scope: LintScope, standalone_repos: Any) -> list[LintCheckOutcome]:
        self.calls.append((scope, standalone_repos))
        return list(self._outcomes)


class _FakeRepoFactory:
    def get_standalone_repos(self) -> list[str]:
        return ["repo-a"]


class _RecordingReporter:
    def __init__(self) -> None:
        self.started_scope: LintScope | None = None
        self.findings: list[LintFinding] = []
        self.summary: LintSummary | None = None

    def started(self, scope: LintScope) -> None:
        self.started_scope = scope

    def finding(self, finding: LintFinding) -> None:
        self.findings.append(finding)

    def finished(self, summary: LintSummary) -> None:
        self.summary = summary


def _make(
    workspace: LintCheckOutcome | None,
    extensions: list[LintCheckOutcome],
    core: LintCheckOutcome | None = None,
) -> tuple[LintService, _RecordingReporter]:
    core_outcomes = [core] if core is not None else []
    svc = LintService(
        core_lint_svc=_FakeCoreLint(core_outcomes),  # type: ignore[arg-type]
        workspace_lint_svc=_FakeScalarLint(workspace),  # type: ignore[arg-type]
        extension_lint_svc=_FakeExtensionLint(extensions),  # type: ignore[arg-type]
        repo_factory=_FakeRepoFactory(),  # type: ignore[arg-type]
    )
    return svc, _RecordingReporter()


def test_aggregates_findings_and_counts_contributors() -> None:
    workspace = LintCheckOutcome("project", [_finding("project", LintStatus.pass_)])
    ext = LintCheckOutcome("wln", [_finding("wln", LintStatus.warn), _finding("wln", LintStatus.fail)])
    svc, reporter = _make(workspace, [ext])

    summary = svc.run(SCOPE, reporter)  # type: ignore[arg-type]

    assert reporter.started_scope is SCOPE
    assert len(reporter.findings) == 3
    assert summary.contributors == 2
    assert summary.total == 3
    assert summary.fails == 1
    assert summary.warns == 1
    assert summary.exit_code == 1
    assert reporter.summary == summary


def test_no_contributors_reports_zero() -> None:
    svc, reporter = _make(None, [])
    summary = svc.run(SCOPE, reporter)  # type: ignore[arg-type]
    assert summary.contributors == 0
    assert summary.total == 0
    assert summary.exit_code == 0
    assert reporter.findings == []


def test_workspace_findings_emit_before_extension_findings() -> None:
    workspace = LintCheckOutcome("project", [_finding("project", LintStatus.pass_)])
    ext = LintCheckOutcome("wln", [_finding("wln", LintStatus.pass_)])
    svc, reporter = _make(workspace, [ext])
    svc.run(SCOPE, reporter)  # type: ignore[arg-type]
    assert [f.source for f in reporter.findings] == ["project", "wln"]


def test_core_findings_emit_before_workspace_and_extension_findings() -> None:
    core = LintCheckOutcome("core", [_finding("core", LintStatus.fail)])
    workspace = LintCheckOutcome("project", [_finding("project", LintStatus.pass_)])
    ext = LintCheckOutcome("wln", [_finding("wln", LintStatus.pass_)])
    svc, reporter = _make(workspace, [ext], core=core)
    summary = svc.run(SCOPE, reporter)  # type: ignore[arg-type]
    assert [f.source for f in reporter.findings] == ["core", "project", "wln"]
    assert summary.contributors == 3


def test_core_runs_even_with_no_workspace_or_extension_checks() -> None:
    core = LintCheckOutcome("core", [_finding("core", LintStatus.fail)])
    svc, reporter = _make(None, [], core=core)
    summary = svc.run(SCOPE, reporter)  # type: ignore[arg-type]
    assert [f.source for f in reporter.findings] == ["core"]
    assert summary.contributors == 1
    assert summary.exit_code == 1


def test_only_warnings_keeps_exit_zero() -> None:
    ext = LintCheckOutcome("wln", [_finding("wln", LintStatus.warn)])
    svc, reporter = _make(None, [ext])
    summary = svc.run(SCOPE, reporter)  # type: ignore[arg-type]
    assert summary.exit_code == 0
    assert summary.warns == 1


def test_passes_standalone_repos_to_extension_service() -> None:
    ext_svc = _FakeExtensionLint([])
    svc = LintService(
        core_lint_svc=_FakeCoreLint([]),  # type: ignore[arg-type]
        workspace_lint_svc=_FakeScalarLint(None),  # type: ignore[arg-type]
        extension_lint_svc=ext_svc,  # type: ignore[arg-type]
        repo_factory=_FakeRepoFactory(),  # type: ignore[arg-type]
    )
    svc.run(SCOPE, _RecordingReporter())  # type: ignore[arg-type]
    assert ext_svc.calls == [(SCOPE, ["repo-a"])]
