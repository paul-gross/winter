from __future__ import annotations

import sys
from typing import Any

from winter_cli.modules.ext.models import NewParams, VerifyParams
from winter_cli.modules.ext.scaffold_service import ExtScaffoldService
from winter_cli.modules.ext.verify_reporter import IVerifyReporter, JsonVerifyReporter, StreamVerifyReporter
from winter_cli.modules.ext.verify_service import ConformanceVerifyService


class ExtVerifyHandler:
    """Dispatches `winter ext verify` runs: resolve, run checks, render, exit non-zero on failure.

    `params.extensions` is one or more names/paths — no glob support (a
    name/path isn't a registry enumeration to expand). Each is verified in
    turn against the same `ConformanceVerifyService`, which still only knows
    how to verify one extension per call; the fan-out lives here. A
    single-target run's output is unchanged from before multi-target support
    (the reporter is called with `extension=None`); a multi-target run labels
    each render with its extension name.
    """

    def __init__(
        self,
        verify_service: ConformanceVerifyService,
        stream_reporter: StreamVerifyReporter,
        json_reporter: JsonVerifyReporter,
    ) -> None:
        self._verify_service = verify_service
        self._stream_reporter = stream_reporter
        self._json_reporter = json_reporter

    def run(self, params: VerifyParams) -> None:
        reporter: IVerifyReporter = self._json_reporter if params.output_json else self._stream_reporter
        sectioned = len(params.extensions) > 1

        any_failed = False
        for extension in params.extensions:
            report = self._verify_service.verify(extension)
            reporter.render(report, extension=extension if sectioned else None)
            any_failed = any_failed or report.any_failed

        if any_failed:
            sys.exit(1)


class ExtNewHandler:
    """Dispatches `winter ext new` runs: scaffold an extension, then report created files."""

    def __init__(self, scaffold_service: ExtScaffoldService, click: Any) -> None:
        self._scaffold_service = scaffold_service
        self._click = click

    def run(self, params: NewParams) -> None:
        try:
            result = self._scaffold_service.scaffold(params)
        except FileExistsError as exc:
            self._click.echo(f"error: {exc}", err=True)
            sys.exit(1)

        self._click.echo(f"Created extension '{params.name}' at {result.output_dir}:")
        for path in result.created_files:
            self._click.echo(f"  {path}")
