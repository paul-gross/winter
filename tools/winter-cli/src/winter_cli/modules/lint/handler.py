from __future__ import annotations

import dataclasses
import sys

import click

from winter_cli.modules.lint.lint_reporter import ILintReporter, JsonLintReporter, StreamLintReporter
from winter_cli.modules.lint.lint_service import LintService
from winter_cli.modules.lint.models import LintScopeKind, LintScopeRequest, LintSummary
from winter_cli.modules.lint.scope_resolver import LintScopeResolver


@dataclasses.dataclass
class LintParams:
    scope: LintScopeRequest
    output_json: bool


class LintHandler:
    """Dispatches `winter lint` runs: resolves the scope(s), then runs the service per scope."""

    def __init__(
        self,
        lint_service: LintService,
        scope_resolver: LintScopeResolver,
        stream_reporter: StreamLintReporter,
        json_reporter: JsonLintReporter,
    ) -> None:
        self._lint_service = lint_service
        self._scope_resolver = scope_resolver
        self._stream_reporter = stream_reporter
        self._json_reporter = json_reporter

    def run(self, params: LintParams) -> None:
        scopes = self._scope_resolver.resolve(params.scope)
        reporter: ILintReporter = self._json_reporter if params.output_json else self._stream_reporter

        if not scopes:
            # Only reachable when every given scope name/glob matched nothing —
            # the default/--all/--changed branches always resolve to at least
            # one scope. Not an error: report and no-op, same as an unmatched
            # glob elsewhere (`provision`, `ws destroy`).
            click.echo(f"No scope matched: {' '.join(params.scope.names)}")
            return

        exit_code = 0
        for scope in scopes:
            # A `--changed` run with no changed files is a clean no-op: emit the
            # scope header and a zero-contributor summary rather than dispatching
            # scripts with an empty WINTER_LINT_PATHS (which would make naive lint
            # scripts fall back to scanning the whole tree).
            if scope.kind == LintScopeKind.changed and not scope.paths:
                empty_summary = LintSummary(contributors=0, total=0, fails=0, warns=0)
                reporter.started(scope)
                reporter.finished(empty_summary)
                continue

            summary = self._lint_service.run(scope, reporter)
            if summary.exit_code != 0:
                exit_code = summary.exit_code

        if exit_code != 0:
            sys.exit(exit_code)
