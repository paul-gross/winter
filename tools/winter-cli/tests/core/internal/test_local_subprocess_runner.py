from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from winter_cli.core.internal import local_subprocess_runner
from winter_cli.core.internal.local_subprocess_runner import LocalSubprocessRunner


def test_run_passes_cmd_cwd_env_to_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="hello\n", stderr="")
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    result = LocalSubprocessRunner.run(["echo", "hi"], cwd=tmp_path, env={"K": "V"})

    fake_subprocess.run.assert_called_once_with(
        ["echo", "hi"],
        cwd=str(tmp_path),
        env={"K": "V"},
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "hello\n"


def test_run_without_cwd_or_env_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    result = LocalSubprocessRunner.run(["git", "status"])

    fake_subprocess.run.assert_called_once_with(
        ["git", "status"],
        cwd=None,
        env=None,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert result.returncode == 0


def test_run_returns_failure_result_when_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = OSError("no such file")
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    result = LocalSubprocessRunner.run(["does-not-exist"])

    assert result.returncode == -1
    assert result.stdout == ""
    assert "no such file" in result.stderr


def test_run_decodes_non_utf8_output_instead_of_raising() -> None:
    """Output that isn't valid UTF-8 comes back as a result, not an exception.

    Real subprocess, no mock: the decode happens inside `subprocess.run`, so
    a fake `subprocess` module would assert nothing. Callers inspect
    `returncode` and are not prepared for `UnicodeDecodeError` from a command
    that succeeded — `git check-ignore` printing a filename from a vendored
    tree is the case that surfaced it.
    """
    result = LocalSubprocessRunner.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"])

    assert result.returncode == 0
    assert "\ufffd" in result.stdout


def test_call_inherits_stdio_and_returns_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`call` does not capture output (stdio inherited) and returns the exit code."""
    fake_subprocess = MagicMock()
    fake_subprocess.run.return_value = MagicMock(returncode=3)
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    code = LocalSubprocessRunner.call(["svc", "up", "alpha"], cwd=tmp_path, env={"K": "V"})

    fake_subprocess.run.assert_called_once_with(
        ["svc", "up", "alpha"],
        cwd=str(tmp_path),
        env={"K": "V"},
        check=False,
    )
    assert code == 3


def test_call_returns_126_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess = MagicMock()
    fake_subprocess.run.side_effect = OSError("not executable")
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    assert LocalSubprocessRunner.call(["does-not-exist"]) == 126


def test_popen_passes_cmd_cwd_env_shell_to_popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["line1\n", "line2\n"])
    fake_proc.wait.return_value = 0
    fake_proc.poll.return_value = 0

    fake_subprocess = MagicMock()
    fake_subprocess.Popen.return_value = fake_proc
    fake_subprocess.PIPE = -1
    fake_subprocess.STDOUT = -2
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    runner = LocalSubprocessRunner()
    with runner.popen(["bash", "-c", "echo hi"], cwd=tmp_path, env={"X": "1"}, shell=False) as proc:
        lines = list(proc.stdout_lines)

    fake_subprocess.Popen.assert_called_once_with(
        ["bash", "-c", "echo hi"],
        cwd=str(tmp_path),
        env={"X": "1"},
        shell=False,
        stdout=fake_subprocess.PIPE,
        stderr=fake_subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert lines == ["line1", "line2"]


def test_popen_merge_stderr_false_passes_none_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """`popen(merge_stderr=False)` sets stderr=None so the child inherits the parent's stderr fd."""
    fake_proc = MagicMock()
    fake_proc.stdout = iter([])
    fake_proc.wait.return_value = 0
    fake_proc.poll.return_value = 0

    fake_subprocess = MagicMock()
    fake_subprocess.Popen.return_value = fake_proc
    fake_subprocess.PIPE = -1
    fake_subprocess.STDOUT = -2
    monkeypatch.setattr(local_subprocess_runner, "subprocess", fake_subprocess)

    runner = LocalSubprocessRunner()
    with runner.popen(["svc", "logs", "alpha"], merge_stderr=False) as _proc:
        pass

    fake_subprocess.Popen.assert_called_once_with(
        ["svc", "logs", "alpha"],
        cwd=None,
        env=None,
        shell=False,
        stdout=fake_subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
