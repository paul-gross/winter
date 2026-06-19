from __future__ import annotations

# Tests for the resolve_output_dir pure helper and the CLI boundary behaviour
# that reads WINTER_INVOCATION_CWD.
#
# Coverage:
#   Unit — resolve_output_dir:
#     no --dir   → invocation_cwd / name
#     absolute --dir → as-is
#     relative --dir → invocation_cwd / relative
#   CLI boundary — ext new with WINTER_INVOCATION_CWD set:
#     extension lands under the invocation cwd, not the process cwd
import os
import subprocess
import sys
from pathlib import Path

from winter_cli.modules.ext.command import resolve_output_dir

# ── pure helper unit tests ────────────────────────────────────────────────────


def test_resolve_no_dir_uses_invocation_cwd(tmp_path: Path) -> None:
    result = resolve_output_dir(tmp_path, "my-ext", None)
    assert result == tmp_path / "my-ext"


def test_resolve_absolute_dir_used_as_is(tmp_path: Path) -> None:
    absolute = tmp_path / "custom" / "location"
    result = resolve_output_dir(tmp_path, "my-ext", str(absolute))
    assert result == absolute


def test_resolve_relative_dir_resolved_against_invocation_cwd(tmp_path: Path) -> None:
    result = resolve_output_dir(tmp_path, "my-ext", "subdir/my-ext")
    assert result == tmp_path / "subdir" / "my-ext"


# ── CLI boundary test: WINTER_INVOCATION_CWD resolution ──────────────────────


def _make_workspace(base: Path) -> Path:
    """Create a minimal .winter/ workspace at *base* and return *base*."""
    base.mkdir(parents=True, exist_ok=True)
    (base / ".winter").mkdir()
    (base / ".winter" / "config.toml").write_text(
        'main_branch = "main"\nsession_prefix = "test"\n'
        '[[project_repository]]\nname = "demo"\nurl = "git@example.com:x/demo.git"\n'
    )
    return base


def _run_ext_new(
    workspace: Path,
    invocation_cwd: Path,
    extra_args: list[str],
) -> subprocess.CompletedProcess:
    """Run ext new with WINTER_INVOCATION_CWD set.

    The subprocess cwd is set to the workspace root (so the workspace locator
    finds .winter/) while WINTER_INVOCATION_CWD carries the user's real dir
    (simulating what the bin/winter launcher does before it cd's to tools/winter-cli/).
    """
    env = {**os.environ, "WINTER_INVOCATION_CWD": str(invocation_cwd)}
    return subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "new", "test-ext",
         "--capability", "service", *extra_args],
        capture_output=True,
        text=True,
        cwd=str(workspace),
        env=env,
    )


def test_ext_new_uses_winter_invocation_cwd(tmp_path: Path) -> None:
    """When WINTER_INVOCATION_CWD is set, ext new creates the extension there.

    The process cwd is the workspace root (tools/winter-cli/ in production),
    while WINTER_INVOCATION_CWD is a separate temp directory representing where
    the user ran the command.  The extension must land under the invocation cwd.
    """
    workspace = _make_workspace(tmp_path / "ws")
    invocation_cwd = tmp_path / "user_dir"
    invocation_cwd.mkdir()

    result = _run_ext_new(workspace, invocation_cwd, [])
    assert result.returncode == 0, (
        f"ext new failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )

    # Extension must exist under invocation_cwd, not the workspace (process cwd).
    assert (invocation_cwd / "test-ext" / "winter-ext.toml").exists(), (
        f"extension not found under invocation cwd {invocation_cwd}; "
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert not (workspace / "test-ext").exists(), (
        "extension was incorrectly created under process cwd (workspace root)"
    )


def test_ext_new_relative_dir_resolves_against_invocation_cwd(tmp_path: Path) -> None:
    """A relative --dir is resolved against WINTER_INVOCATION_CWD, not process cwd."""
    workspace = _make_workspace(tmp_path / "ws")
    invocation_cwd = tmp_path / "user_dir"
    invocation_cwd.mkdir()

    result = _run_ext_new(workspace, invocation_cwd, ["--dir", "relative/output"])
    assert result.returncode == 0, (
        f"ext new failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )

    assert (invocation_cwd / "relative" / "output" / "winter-ext.toml").exists(), (
        "relative --dir was not resolved against invocation cwd"
    )
    assert not (workspace / "relative").exists(), (
        "relative --dir was incorrectly resolved against process cwd"
    )


def test_ext_new_absolute_dir_used_as_is(tmp_path: Path) -> None:
    """An absolute --dir is used as-is regardless of invocation cwd."""
    workspace = _make_workspace(tmp_path / "ws")
    invocation_cwd = tmp_path / "user_dir"
    invocation_cwd.mkdir()
    absolute_out = tmp_path / "absolute_output"

    result = _run_ext_new(workspace, invocation_cwd, ["--dir", str(absolute_out)])
    assert result.returncode == 0, (
        f"ext new failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )

    assert (absolute_out / "winter-ext.toml").exists(), (
        "absolute --dir was not honoured as-is"
    )
