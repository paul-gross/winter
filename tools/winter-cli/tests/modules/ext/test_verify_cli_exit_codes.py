from __future__ import annotations

# CLI-boundary smoke tests for `winter ext verify` exit codes.
#
# The service-layer tests in test_verify_service.py cover all failure classes at
# the Python API level.  This file adds a thin CLI-boundary layer that calls the
# real `winter ext verify` CLI via subprocess and asserts the exit codes that
# external consumers (shell scripts, CI) depend on.
#
# Coverage map:
#   exit 0 — conforming provider         → via test_scaffold_service.py::test_scaffold_output_passes_verify
#   exit 1 — setup failure               → test_verify_exits_nonzero_on_setup_failure
#   exit 1 — action-word not accepted    → test_verify_exits_nonzero_on_action_rejected
#   exit 1 — unknown action accepted     → test_verify_exits_nonzero_on_unknown_action_accepted
#   exit 1 — params dropped              → test_verify_exits_nonzero_on_params_dropped
#
# Each failure-class test writes a minimal extension whose entrypoint is a tiny
# Python script that exhibits exactly one failure class; it does NOT use the
# scaffold (that would make it conforming).
import stat
import subprocess
import sys
from pathlib import Path

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_workspace(base: Path) -> Path:
    """Create a minimal .winter/ workspace at *base* and return *base*."""
    base.mkdir(parents=True, exist_ok=True)
    (base / ".winter").mkdir()
    (base / ".winter" / "config.toml").write_text(
        'main_branch = "main"\nservice_prefix = "test"\n'
        '[[project_repository]]\nname = "demo"\nurl = "git@example.com:x/demo.git"\n'
    )
    return base


def _make_ext(
    ext_dir: Path,
    entrypoint_script: str,
) -> Path:
    """Write a minimal extension with the given entrypoint script.

    Returns the ext_dir for convenience.
    """
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "winter-ext.toml").write_text('name = "test-ext"\n\n[provides]\nservice = "workflow/service"\n')
    ep = ext_dir / "workflow" / "service"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(entrypoint_script)
    ep.chmod(ep.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return ext_dir


def _verify(workspace: Path, ext_dir: Path) -> subprocess.CompletedProcess:
    """Run `winter ext verify <ext_dir>` in *workspace* and return the result."""
    return subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", str(ext_dir)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )


# Known actions and their sentinel/unknown-action constants (from service-v1.toml).
_KNOWN_ACTIONS = {"up", "down", "status", "restart", "logs"}
_UNKNOWN_ACTION = "__winter_nonexistent_action__"
_SENTINEL = "__WINTER_VERIFY_SENTINEL__"

# ── conforming extension: exit 0 ──────────────────────────────────────────────
# NOTE: exit-0 coverage is provided by test_scaffold_service.py::test_scaffold_output_passes_verify.
# This file adds the setup-failure and each failure-class non-zero case.


# ── setup failure: extension directory does not exist ─────────────────────────


def test_verify_exits_nonzero_on_setup_failure(tmp_path: Path) -> None:
    """When the extension directory does not exist, exit is non-zero."""
    workspace = _make_workspace(tmp_path / "ws")
    missing_dir = tmp_path / "nonexistent-ext"

    result = _verify(workspace, missing_dir)

    assert result.returncode != 0, (
        f"expected non-zero exit for missing extension directory; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ── failure class A: action-word not accepted (exits 2 for a declared action) ─


def test_verify_exits_nonzero_on_action_rejected(tmp_path: Path) -> None:
    """When the entrypoint exits 2 for a declared action, `ext verify` exits non-zero.

    This is the 'action-word-not-accepted' failure class: the entrypoint returns
    the unknown-action signal (exit 2) even for a valid declared action word.
    """
    workspace = _make_workspace(tmp_path / "ws")
    # Entrypoint always exits 2 (unknown-action) — rejects everything including
    # the declared action words, so 'accepts-*' checks fail.
    rejects_all = "#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n"
    ext_dir = _make_ext(tmp_path / "ext", rejects_all)

    result = _verify(workspace, ext_dir)

    assert result.returncode != 0, (
        f"expected non-zero exit when action rejected; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ── failure class B: unknown action accepted (exits 0 for unknown action) ─────


def test_verify_exits_nonzero_on_unknown_action_accepted(tmp_path: Path) -> None:
    """When the entrypoint exits 0 for an unknown action, `ext verify` exits non-zero.

    This is the 'unknown-action-accepted' failure class: the refuses-unknown check
    expects a non-zero exit for the probe unknown action, but gets exit 0.
    """
    workspace = _make_workspace(tmp_path / "ws")
    # Entrypoint always exits 0 — accepts everything including unknown actions.
    # Also echoes argv to stderr so forwards-params passes.
    accepts_all = "#!/usr/bin/env python3\nimport sys\nprint(' '.join(sys.argv), file=sys.stderr)\nsys.exit(0)\n"
    ext_dir = _make_ext(tmp_path / "ext", accepts_all)

    result = _verify(workspace, ext_dir)

    assert result.returncode != 0, (
        f"expected non-zero exit when unknown action accepted; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "refuses-unknown" in combined or "✗" in combined, "expected 'refuses-unknown' failure in output"


# ── failure class C: params dropped (sentinel not echoed back) ───────────────


def test_verify_exits_nonzero_on_params_dropped(tmp_path: Path) -> None:
    """When the entrypoint does not echo the sentinel, `ext verify` exits non-zero.

    This is the 'params-dropped' failure class: the forwards-params check sends a
    sentinel token in the argv but the entrypoint produces no output containing it.
    """
    workspace = _make_workspace(tmp_path / "ws")
    # Entrypoint accepts known actions, refuses unknown, but produces NO output —
    # so the sentinel is never echoed back, making forwards-params fail.
    known_actions_repr = repr(list(_KNOWN_ACTIONS))
    drops_params = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"_KNOWN = {known_actions_repr}\n"
        "if len(sys.argv) < 2 or sys.argv[1] not in _KNOWN:\n"
        "    sys.exit(2)\n"
        "# Exits 0 for known actions but produces NO stdout/stderr (drops argv/params).\n"
        "sys.exit(0)\n"
    )
    ext_dir = _make_ext(tmp_path / "ext", drops_params)

    result = _verify(workspace, ext_dir)

    assert result.returncode != 0, (
        f"expected non-zero exit when params dropped; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "forwards-params" in combined or "✗" in combined, "expected 'forwards-params' failure in output"


# ── --json flag: exit codes are the same regardless of output format ──────────


def test_verify_json_flag_also_exits_nonzero_on_failure(tmp_path: Path) -> None:
    """--json does not suppress the non-zero exit on failure."""
    workspace = _make_workspace(tmp_path / "ws")
    missing_dir = tmp_path / "nonexistent-ext"

    result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", "--json", str(missing_dir)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )

    assert result.returncode != 0, (
        f"expected non-zero exit for --json + setup failure; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ── multi-target: verify several EXTENSIONs in one run ────────────────────────


def _scaffold_ext(workspace: Path, name: str, out_dir: Path) -> Path:
    """Scaffold a conforming extension via the real `ext new` (passes verify out of the box)."""
    result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "new", name, "--capability", "service", "--dir", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    assert result.returncode == 0, f"ext new failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    return out_dir


def test_verify_multiple_extensions_all_passing_exits_zero(tmp_path: Path) -> None:
    """Multiple EXTENSIONs, all conforming, verify in one run and exit 0."""
    workspace = _make_workspace(tmp_path / "ws")
    ext_a = _scaffold_ext(workspace, "ext-a", tmp_path / "ext-a")
    ext_b = _scaffold_ext(workspace, "ext-b", tmp_path / "ext-b")

    result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", str(ext_a), str(ext_b)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )

    assert result.returncode == 0, f"expected exit 0; got {result.returncode}\nstdout: {result.stdout}"


def test_verify_multiple_extensions_one_failing_exits_nonzero(tmp_path: Path) -> None:
    """One failing EXTENSION among several is enough to make the whole run exit non-zero."""
    workspace = _make_workspace(tmp_path / "ws")
    ext_a = _scaffold_ext(workspace, "ext-a", tmp_path / "ext-a")
    rejects_all = "#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n"
    ext_b = _make_ext(tmp_path / "ext-b", rejects_all)

    result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", str(ext_a), str(ext_b)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )

    assert result.returncode != 0, f"expected non-zero exit; got {result.returncode}\nstdout: {result.stdout}"
    # Both extensions still ran — the failing one didn't short-circuit the other.
    assert str(ext_a) in result.stdout
    assert str(ext_b) in result.stdout


def test_verify_multiple_extensions_json_emits_one_line_per_extension(tmp_path: Path) -> None:
    """--json with multiple EXTENSIONs emits one NDJSON line per extension, each labelled."""
    import json

    workspace = _make_workspace(tmp_path / "ws")
    ext_a = _scaffold_ext(workspace, "ext-a", tmp_path / "ext-a")
    ext_b = _scaffold_ext(workspace, "ext-b", tmp_path / "ext-b")

    result = subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ext", "verify", "--json", str(ext_a), str(ext_b)],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )

    assert result.returncode == 0, f"expected exit 0; got {result.returncode}\nstdout: {result.stdout}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    payloads = [json.loads(ln) for ln in lines]
    assert {p["extension"] for p in payloads} == {str(ext_a), str(ext_b)}
