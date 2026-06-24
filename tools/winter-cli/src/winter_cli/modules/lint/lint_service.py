from __future__ import annotations

from winter_cli.modules.lint.core_lint_service import CoreLintService
from winter_cli.modules.lint.extension_lint_service import ExtensionLintService
from winter_cli.modules.lint.lint_reporter import ILintReporter
from winter_cli.modules.lint.models import LintCheckOutcome, LintScope, LintStatus, LintSummary
from winter_cli.modules.lint.workspace_lint_service import WorkspaceLintService
from winter_cli.modules.workspace.repository_factory import RepositoryFactory


class LintService:
    """Aggregates lint findings from built-in core checks, the workspace script, and every extension's script.

    A pure dispatcher: it owns discovery, ordering, and aggregation, but never
    inspects content itself — every finding originates in a check it dispatches.
    Built-in core checks run first, then the workspace script, then each
    extension in standalone-repo order — mirroring the doctor
    `[core]`-then-`[project]`-then-extensions ordering.
    """

    def __init__(
        self,
        core_lint_svc: CoreLintService,
        workspace_lint_svc: WorkspaceLintService,
        extension_lint_svc: ExtensionLintService,
        repo_factory: RepositoryFactory,
    ) -> None:
        self._core_lint_svc = core_lint_svc
        self._workspace_lint_svc = workspace_lint_svc
        self._extension_lint_svc = extension_lint_svc
        self._repo_factory = repo_factory

    def run(self, scope: LintScope, reporter: ILintReporter) -> LintSummary:
        reporter.started(scope)

        outcomes: list[LintCheckOutcome] = []
        outcomes.extend(self._core_lint_svc.run(scope))
        workspace_outcome = self._workspace_lint_svc.run(scope)
        if workspace_outcome is not None:
            outcomes.append(workspace_outcome)

        standalone_repos = self._repo_factory.get_standalone_repos()
        outcomes.extend(self._extension_lint_svc.run(scope, standalone_repos))

        findings = [finding for outcome in outcomes for finding in outcome.findings]
        for finding in findings:
            reporter.finding(finding)

        fails = sum(1 for f in findings if f.status == LintStatus.fail)
        warns = sum(1 for f in findings if f.status == LintStatus.warn)
        summary = LintSummary(contributors=len(outcomes), total=len(findings), fails=fails, warns=warns)
        reporter.finished(summary)
        return summary
