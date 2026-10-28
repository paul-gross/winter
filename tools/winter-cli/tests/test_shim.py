"""The launcher shim (`bin/winter`), run for real against a scratch workspace with stub `uv` and venv entrypoint."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "bin" / "winter"

_ENTRYPOINT = """#!/bin/sh
echo "ENTRYPOINT args=$*"
echo "VIRTUAL_ENV=$VIRTUAL_ENV"
echo "PATH_HEAD=${PATH%%:*}"
"""

_UV = """#!/bin/sh
echo "$*" >> "$UV_LOG"
echo "SYNC-OUTPUT"
if [ "$1" = sync ]; then
  venv="${UV_PROJECT_ENVIRONMENT:-$3/.venv}"
  mkdir -p "$venv/bin"
  printf '%s' "$ENTRYPOINT_BODY" > "$venv/bin/winter"
  chmod +x "$venv/bin/winter"
fi
"""


class _Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cli = root / "ws" / "tools" / "winter-cli"
        self.venv = self.cli / ".venv"
        self.uv_log = root / "uv.log"
        self.cli.mkdir(parents=True)
        (root / "ws" / ".winter").mkdir()
        (root / "ws" / ".winter" / "config.toml").touch()
        for name in ("uv.lock", "pyproject.toml", ".python-version"):
            (self.cli / name).touch()
        stub = root / "stub"
        stub.mkdir()
        (stub / "uv").write_text(_UV)
        (stub / "uv").chmod(0o755)
        self.stub = stub

    def seed_current_venv(self) -> None:
        (self.venv / "bin").mkdir(parents=True)
        entry = self.venv / "bin" / "winter"
        entry.write_text(_ENTRYPOINT)
        entry.chmod(0o755)
        stamp = self.venv / ".winter-sync-stamp"
        stamp.touch()
        future = stamp.stat().st_mtime + 100
        os.utime(stamp, (future, future))

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.stub}:{os.environ['PATH']}",
            "UV_LOG": str(self.uv_log),
            "ENTRYPOINT_BODY": _ENTRYPOINT,
        }
        env.pop("UV_PROJECT_ENVIRONMENT", None)
        return subprocess.run(
            ["bash", str(SHIM), *args], cwd=self.root / "ws", env=env, capture_output=True, text=True, check=False
        )

    def uv_calls(self) -> list[str]:
        return self.uv_log.read_text().splitlines() if self.uv_log.exists() else []


@pytest.fixture
def scratch(tmp_path: Path) -> _Scratch:
    return _Scratch(tmp_path)


def test_current_venv_execs_the_entrypoint_without_invoking_uv(scratch: _Scratch) -> None:
    scratch.seed_current_venv()

    result = scratch.run("ws", "status")

    assert result.returncode == 0, result.stderr
    assert "ENTRYPOINT args=ws status" in result.stdout
    assert scratch.uv_calls() == []


def test_exec_sees_the_venv_environment_uv_run_would_set(scratch: _Scratch) -> None:
    scratch.seed_current_venv()

    result = scratch.run("--version")

    assert f"VIRTUAL_ENV={scratch.venv}" in result.stdout
    assert f"PATH_HEAD={scratch.venv}/bin" in result.stdout


@pytest.mark.parametrize("newer_input", ["uv.lock", "pyproject.toml", ".python-version"])
def test_an_input_newer_than_the_stamp_syncs_first_then_stops_syncing(scratch: _Scratch, newer_input: str) -> None:
    scratch.seed_current_venv()
    path = scratch.cli / newer_input
    newer = (scratch.venv / ".winter-sync-stamp").stat().st_mtime + 1000
    os.utime(path, (newer, newer))

    first = scratch.run("--version")
    assert first.returncode == 0, first.stderr
    assert scratch.uv_calls() == [f"sync --project {scratch.cli}"]
    assert "ENTRYPOINT" in first.stdout

    # The sync re-stamps at the real clock; move that stamp past the future-dated input.
    stamp = scratch.venv / ".winter-sync-stamp"
    later = path.stat().st_mtime + 10
    os.utime(stamp, (later, later))
    scratch.run("--version")
    assert len(scratch.uv_calls()) == 1


def test_a_missing_venv_syncs_first(scratch: _Scratch) -> None:
    result = scratch.run("ws", "status")

    assert result.returncode == 0, result.stderr
    assert scratch.uv_calls() == [f"sync --project {scratch.cli}"]
    assert "ENTRYPOINT args=ws status" in result.stdout
    assert (scratch.venv / ".winter-sync-stamp").exists()


def test_sync_output_goes_to_stderr_only(scratch: _Scratch) -> None:
    result = scratch.run("--version")

    assert "SYNC-OUTPUT" in result.stderr
    assert "SYNC-OUTPUT" not in result.stdout


def test_uv_project_environment_relocates_the_venv(scratch: _Scratch, tmp_path: Path) -> None:
    relocated = tmp_path / "elsewhere" / "venv"
    env_result = subprocess.run(
        ["bash", str(SHIM), "--version"],
        cwd=scratch.root / "ws",
        env={
            **os.environ,
            "PATH": f"{scratch.stub}:{os.environ['PATH']}",
            "UV_LOG": str(scratch.uv_log),
            "ENTRYPOINT_BODY": _ENTRYPOINT,
            "UV_PROJECT_ENVIRONMENT": str(relocated),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert env_result.returncode == 0, env_result.stderr
    assert f"VIRTUAL_ENV={relocated}" in env_result.stdout


def test_shim_is_valid_bash() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    assert subprocess.run([bash, "-n", str(SHIM)], check=False).returncode == 0
