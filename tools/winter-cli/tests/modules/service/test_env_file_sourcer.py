"""Unit tests for ShellEnvFileSourcer (IEnvFileSourcer adapter).

Covers:
  - Plain variable sourcing from a fixture .winter.env
  - Arithmetic-derived variable evaluation (set -a arithmetic expansion)
  - Workspace-scope file selection (.winter.workspace.env)
  - Absent file → empty dict (no exception)
  - No-ambient-leak: only variables defined by the file are returned
  - Shell failure (syntax error) → EnvFileSourcerError
  - Newline-safe: values containing embedded newlines round-trip intact

These tests use the real subprocess (bash) rather than mocking it so the
shell-sourcing semantics (arithmetic evaluation, set -a allexport, env -0
NUL-delimited output) are truly exercised.  The adapter is tested at the
integration level for this specific reason — mock-based tests would only
check that subprocess was called with the right args, not that the arithmetic
was actually evaluated or that newlines are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winter_cli.modules.service.env_file_sourcer import EnvFileSourcerError
from winter_cli.modules.service.internal.shell_env_file_sourcer import ShellEnvFileSourcer


@pytest.fixture
def sourcer() -> ShellEnvFileSourcer:
    return ShellEnvFileSourcer()


def test_sources_plain_variable(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """A plain KEY=VALUE assignment in .winter.env is returned in the dict."""
    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("WINTER_PORT_BASE=4060\n")

    result = sourcer.source("alpha", tmp_path)

    assert result["WINTER_PORT_BASE"] == "4060"


def test_evaluates_arithmetic(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """Arithmetic expressions are evaluated by bash before the result is returned."""
    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("WINTER_PORT_BASE=4060\nWTS_DB_PORT=$((WINTER_PORT_BASE+12))\n")

    result = sourcer.source("alpha", tmp_path)

    assert result["WINTER_PORT_BASE"] == "4060"
    assert result["WTS_DB_PORT"] == "4072"


def test_workspace_scope_reads_workspace_env_file(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """scope='workspace' selects <ws_root>/.winter.workspace.env."""
    (tmp_path / ".winter.workspace.env").write_text("WINTER_WORKSPACE_PORT=9000\n")

    result = sourcer.source("workspace", tmp_path)

    assert result["WINTER_WORKSPACE_PORT"] == "9000"


def test_absent_env_file_returns_empty_dict(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """When the env file does not exist, returns an empty dict (no exception)."""
    (tmp_path / "alpha").mkdir()
    # No .winter.env written.

    result = sourcer.source("alpha", tmp_path)

    assert result == {}


def test_absent_env_dir_returns_empty_dict(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """When even the env directory doesn't exist, returns empty dict."""
    result = sourcer.source("nonexistent-env", tmp_path)

    assert result == {}


def test_absent_workspace_env_file_returns_empty_dict(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """Absent .winter.workspace.env → empty dict."""
    # No .winter.workspace.env written.
    result = sourcer.source("workspace", tmp_path)

    assert result == {}


def test_no_ambient_leak(tmp_path: Path, sourcer: ShellEnvFileSourcer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Variables from the caller's environment do NOT appear in the returned dict.

    The subprocess starts with env={} so only file-defined variables are present.
    This test injects a sentinel variable into the current process environment
    and asserts it is absent from the sourcer's return value.
    """
    monkeypatch.setenv("WINTER_TEST_LEAK_SENTINEL", "should-not-appear")

    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("WINTER_PORT_BASE=4060\n")

    result = sourcer.source("alpha", tmp_path)

    assert "WINTER_TEST_LEAK_SENTINEL" not in result
    assert result["WINTER_PORT_BASE"] == "4060"


def test_shell_failure_raises_env_file_sourcer_error(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """A syntax error in the env file causes the bash subprocess to exit non-zero.

    ShellEnvFileSourcer must raise EnvFileSourcerError in that case.
    """
    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    # Unclosed substitution causes a bash parse error.
    (env_dir / ".winter.env").write_text("BROKEN=$(\n")

    with pytest.raises(EnvFileSourcerError) as exc_info:
        sourcer.source("alpha", tmp_path)

    assert exc_info.value.exit_code != 0


def test_multiple_variables(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """Multiple variables in the file are all captured."""
    env_dir = tmp_path / "beta"
    env_dir.mkdir()
    (env_dir / ".winter.env").write_text("WINTER_PORT_BASE=5000\nAPP_NAME=myapp\nDERIVED=$((WINTER_PORT_BASE+1))\n")

    result = sourcer.source("beta", tmp_path)

    assert result["WINTER_PORT_BASE"] == "5000"
    assert result["APP_NAME"] == "myapp"
    assert result["DERIVED"] == "5001"


def test_newline_in_value_round_trips_intact(tmp_path: Path, sourcer: ShellEnvFileSourcer) -> None:
    """A variable whose value contains an embedded newline is preserved intact.

    The NUL-delimited ``env -0`` output keeps the full value across newlines,
    achieving byte-parity with how providers ``source`` the same file.
    """
    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    # Use $'...' quoting to embed a literal newline in the variable value.
    (env_dir / ".winter.env").write_text("MULTILINE=$'line1\\nline2'\n")

    result = sourcer.source("alpha", tmp_path)

    assert result["MULTILINE"] == "line1\nline2"
