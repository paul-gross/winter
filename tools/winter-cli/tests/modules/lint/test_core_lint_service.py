from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem, FakeSubprocessRunner
from winter_cli.config.models import FileSizeLintConfig
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.lint.core_lint_service import (
    CORE_SOURCE,
    FILE_SIZE_CHECK,
    CoreLintService,
    FileSizeLintCheck,
    default_extractability_script_path,
)
from winter_cli.modules.lint.models import LintScope, LintScopeKind, LintStatus

WORKSPACE_ROOT = Path("/ws")
SCRIPT_PATH = Path("/cli/tools/winter-lint/extractability.py")
SCOPE = LintScope(kind=LintScopeKind.all, label="all", paths=[WORKSPACE_ROOT])

# The fake runner keys responses by the joined command string.
_CMD_KEY = f"{sys.executable} {SCRIPT_PATH}"


def _build_service(
    *,
    files: dict[Path, str] | None = None,
    run_response: SubprocessResult | None = None,
    file_size_config: FileSizeLintConfig | None = None,
) -> tuple[CoreLintService, FakeSubprocessRunner]:
    fs = FakeFilesystem(
        files=files if files is not None else {SCRIPT_PATH: ""},
        directories={WORKSPACE_ROOT},
    )
    responses: dict[str, SubprocessResult] = {}
    if run_response is not None:
        responses[_CMD_KEY] = run_response
    runner = FakeSubprocessRunner(run_responses=responses)
    svc = CoreLintService(
        workspace_root=WORKSPACE_ROOT,
        fs=fs,
        subprocess_runner=runner,
        winter_cli_path="/usr/bin/winter",
        script_path=SCRIPT_PATH,
        file_size_config=file_size_config,
    )
    return svc, runner


def test_runs_bundled_script_with_lint_env() -> None:
    svc, runner = _build_service(run_response=SubprocessResult(0, "", ""))
    svc.run(SCOPE)
    assert runner.run_calls[-1][0] == [sys.executable, str(SCRIPT_PATH)]
    assert runner.run_calls[-1][1] == WORKSPACE_ROOT
    env = runner.run_envs[-1]
    assert env is not None
    assert env["WINTER_CLI"] == "/usr/bin/winter"
    assert env["WINTER_WORKSPACE_DIR"] == str(WORKSPACE_ROOT)
    assert env["WINTER_LINT_SCOPE"] == "all"
    assert env["WINTER_LINT_PATHS"] == str(WORKSPACE_ROOT)


def test_parses_findings_under_core_source() -> None:
    svc, _ = _build_service(
        run_response=SubprocessResult(
            0,
            '{"check": "extractability", "status": "fail", "message": "layering", "file": "ai/x.md", "line": 3}\n',
            "",
        )
    )
    outcomes = svc.run(SCOPE)
    assert outcomes
    # extractability outcome is first
    outcome = outcomes[0]
    assert outcome.source == CORE_SOURCE
    finding = outcome.findings[0]
    assert finding.source == CORE_SOURCE
    assert finding.check == "extractability"
    assert finding.status == LintStatus.fail
    assert finding.file == "ai/x.md"
    assert finding.line == 3


def test_clean_run_still_contributes_outcomes() -> None:
    svc, _ = _build_service(run_response=SubprocessResult(0, "", ""))
    outcomes = svc.run(SCOPE)
    # extractability, file-size, and required-services outcomes are all present.
    assert len(outcomes) == 3
    assert all(o.source == CORE_SOURCE for o in outcomes)


def test_non_zero_exit_becomes_synthetic_fail() -> None:
    svc, _ = _build_service(run_response=SubprocessResult(1, "", "graph fetch failed"))
    outcomes = svc.run(SCOPE)
    assert outcomes
    assert outcomes[0].findings[0].status == LintStatus.fail
    assert outcomes[0].findings[0].message == "graph fetch failed"


def test_missing_script_contributes_nothing() -> None:
    svc, runner = _build_service(files={})
    assert svc.run(SCOPE) == []
    assert runner.run_calls == []


def test_default_script_path_points_at_sibling_tools_dir() -> None:
    path = default_extractability_script_path()
    assert path.parts[-3:] == ("tools", "winter-lint", "extractability.py")


# ── FileSizeLintCheck tests ──────────────────────────────────────────────────


@pytest.fixture
def tmp_ws(tmp_path: Path) -> Path:
    """A scratch workspace directory on the real filesystem."""
    return tmp_path


def test_file_under_threshold_passes(tmp_ws: Path) -> None:
    """A markdown file smaller than both thresholds produces no findings."""
    md = tmp_ws / "ai" / "small.md"
    md.parent.mkdir(parents=True)
    md.write_text("# tiny\n")  # well under any threshold

    check = FileSizeLintCheck(tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000))
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert findings == []


def test_injected_file_over_tighter_threshold_fails(tmp_ws: Path) -> None:
    """A file in the @import graph that exceeds the injected threshold is flagged."""
    # Create CLAUDE.md that @imports ai/index.md
    claude_md = tmp_ws / "CLAUDE.md"
    ai_index = tmp_ws / "ai" / "index.md"
    ai_index.parent.mkdir(parents=True)

    # ai/index.md is over the injected threshold (6000 bytes) but under reference (12000)
    oversized_content = "x" * 7000
    ai_index.write_bytes(oversized_content.encode())

    claude_md.write_text("See @ai/index.md for details.\n")

    check = FileSizeLintCheck(tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000))
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)

    injected_findings = [f for f in findings if f.check == FILE_SIZE_CHECK and "ai/index.md" in (f.file or "")]
    assert injected_findings, f"Expected a finding for ai/index.md; got {findings}"
    f = injected_findings[0]
    assert f.status == LintStatus.fail
    assert "7000" in f.message
    assert "injected" in f.message
    assert "6000" in f.message


def test_non_injected_file_between_thresholds_passes(tmp_ws: Path) -> None:
    """A non-injected reference doc between the two thresholds should not be flagged."""
    # No CLAUDE.md → nothing is injected.
    ref_doc = tmp_ws / "docs" / "reference.md"
    ref_doc.parent.mkdir(parents=True)

    # Between injected (6000) and reference (12000) thresholds.
    between_content = "y" * 8000
    ref_doc.write_bytes(between_content.encode())

    check = FileSizeLintCheck(tmp_ws, FileSizeLintConfig(injected_bytes=6000, reference_bytes=12000))
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert findings == [], f"Expected no findings for a reference doc under 12 000 bytes; got {findings}"


def test_threshold_override_via_config(tmp_ws: Path) -> None:
    """A very tight custom threshold flags a file that the default would pass."""
    md = tmp_ws / "small.md"
    md.write_bytes(b"hello world")  # 11 bytes

    # Override: flag anything over 5 bytes.
    check = FileSizeLintCheck(tmp_ws, FileSizeLintConfig(injected_bytes=5, reference_bytes=5))
    scope = LintScope(kind=LintScopeKind.all, label="all", paths=[tmp_ws])
    findings = check.check(scope)
    assert len(findings) == 1
    assert findings[0].check == FILE_SIZE_CHECK
    assert findings[0].status == LintStatus.fail
