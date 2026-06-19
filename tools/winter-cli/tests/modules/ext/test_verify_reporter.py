from __future__ import annotations

import json

from tests.conftest import ClickRecorder
from winter_cli.modules.ext.models import CheckResult, VerifyReport
from winter_cli.modules.ext.verify_reporter import JsonVerifyReporter, StreamVerifyReporter


def _passing_result(check_id: str = "accepts-up") -> CheckResult:
    return CheckResult(check_id=check_id, passed=True, detail="ok", argv=["ep", "up", "env"], observed_exit=0)


def _failing_result(check_id: str = "refuses-unknown") -> CheckResult:
    return CheckResult(check_id=check_id, passed=False, detail="bad", argv=["ep", "x"], observed_exit=0)


# ── StreamVerifyReporter ─────────────────────────────────────────────────────


def test_stream_reporter_renders_pass_glyph() -> None:
    click = ClickRecorder()
    reporter = StreamVerifyReporter(click=click)
    report = VerifyReport(results=[_passing_result()])
    reporter.render(report)
    output = "\n".join(line for line, _ in click.calls)
    assert "✓" in output


def test_stream_reporter_renders_fail_glyph() -> None:
    click = ClickRecorder()
    reporter = StreamVerifyReporter(click=click)
    report = VerifyReport(results=[_failing_result()])
    reporter.render(report)
    output = "\n".join(line for line, _ in click.calls)
    assert "✗" in output


def test_stream_reporter_setup_failure_goes_to_stderr() -> None:
    click = ClickRecorder()
    reporter = StreamVerifyReporter(click=click)
    report = VerifyReport(setup_failure="ext not found")
    reporter.render(report)
    err_calls = [line for line, is_err in click.calls if is_err]
    assert any("ext not found" in line for line in err_calls)


def test_stream_reporter_summary_line_on_pass() -> None:
    click = ClickRecorder()
    reporter = StreamVerifyReporter(click=click)
    report = VerifyReport(results=[_passing_result()])
    reporter.render(report)
    all_output = "\n".join(line for line, _ in click.calls)
    assert "passed" in all_output


def test_stream_reporter_summary_line_on_fail() -> None:
    click = ClickRecorder()
    reporter = StreamVerifyReporter(click=click)
    report = VerifyReport(results=[_failing_result()])
    reporter.render(report)
    # Summary goes to stderr on failure
    all_stderr = "\n".join(line for line, is_err in click.calls if is_err)
    assert "failed" in all_stderr


# ── JsonVerifyReporter ───────────────────────────────────────────────────────


def test_json_reporter_emits_valid_json() -> None:
    click = ClickRecorder()
    reporter = JsonVerifyReporter(click=click)
    report = VerifyReport(results=[_passing_result()])
    reporter.render(report)
    assert len(click.calls) == 1
    payload = json.loads(click.calls[0][0])
    assert "any_failed" in payload
    assert "results" in payload


def test_json_reporter_setup_failure_in_payload() -> None:
    click = ClickRecorder()
    reporter = JsonVerifyReporter(click=click)
    report = VerifyReport(setup_failure="missing dir")
    reporter.render(report)
    payload = json.loads(click.calls[0][0])
    assert payload["setup_failure"] == "missing dir"
    assert payload["any_failed"] is True


def test_json_reporter_result_shape() -> None:
    click = ClickRecorder()
    reporter = JsonVerifyReporter(click=click)
    result = _passing_result("accepts-up")
    report = VerifyReport(results=[result])
    reporter.render(report)
    payload = json.loads(click.calls[0][0])
    assert payload["results"][0]["check_id"] == "accepts-up"
    assert payload["results"][0]["passed"] is True
    assert payload["results"][0]["observed_exit"] == 0
