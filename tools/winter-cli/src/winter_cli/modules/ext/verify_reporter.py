from __future__ import annotations

import json
from typing import Any, Protocol

from winter_cli.modules.ext.models import CheckResult, VerifyReport


class IVerifyReporter(Protocol):
    """Sink for a verify report — rendered in a single call.

    `extension` is `None` for a single-target run (output is byte-identical
    to before multi-target support), and set to the extension's name when
    `winter ext verify` is fanning out over more than one EXTENSION — each
    per-extension render is then labelled so multi-target output stays
    attributable.
    """

    def render(self, report: VerifyReport, extension: str | None = None) -> None: ...


class StreamVerifyReporter:
    """Renders a VerifyReport as human-readable lines.

    One line per check: `✓ <detail>` or `✗ <detail>`. Setup failures are
    printed as a single error line. A summary line is printed at the end. A
    bold `extension` header is printed first only when `extension` is given
    (the multi-target fan-out case) — a single-target run renders exactly as
    it did before multi-target support.
    """

    def __init__(self, click: Any) -> None:
        self._click = click

    def render(self, report: VerifyReport, extension: str | None = None) -> None:
        if extension is not None:
            self._click.echo(self._click.style(extension, bold=True))

        if report.setup_failure is not None:
            self._click.echo(f"error: {report.setup_failure}", err=True)
            return

        for result in report.results:
            glyph = "✓" if result.passed else "✗"
            self._click.echo(f"  {glyph} {result.detail}")

        total = len(report.results)
        fails = sum(1 for r in report.results if not r.passed)
        if fails:
            self._click.echo(f"\n✗ {fails} failed / {total} total", err=True)
        else:
            self._click.echo(f"\n✓ {total} passed")


class JsonVerifyReporter:
    """Emits a VerifyReport as a single JSON object per `render()` call.

    Stable machine contract:
    {"setup_failure": null|"...", "any_failed": bool,
     "results": [{"check_id": "...", "passed": bool, "detail": "...",
                  "argv": [...], "observed_exit": N}]}

    A multi-target `winter ext verify` run (more than one EXTENSION) calls
    `render()` once per extension with `extension` set, which adds an
    `"extension"` key to that line's payload — one NDJSON line per extension.
    A single-target run omits `extension` and the payload is byte-identical
    to before multi-target support.
    """

    def __init__(self, click: Any) -> None:
        self._click = click

    def render(self, report: VerifyReport, extension: str | None = None) -> None:
        payload: dict[str, Any] = {
            "setup_failure": report.setup_failure,
            "any_failed": report.any_failed,
            "results": [_result_to_dict(r) for r in report.results],
        }
        if extension is not None:
            payload = {"extension": extension, **payload}
        self._click.echo(json.dumps(payload))


def _result_to_dict(r: CheckResult) -> dict[str, Any]:
    return {
        "check_id": r.check_id,
        "passed": r.passed,
        "detail": r.detail,
        "argv": r.argv,
        "observed_exit": r.observed_exit,
    }
