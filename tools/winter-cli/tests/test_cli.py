from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import click
import pytest

from winter_cli import cli as cli_module
from winter_cli.cli import LazyGroup, _bytecode_cache_prefix, _cli_group

# ── LazyGroup (lazy subcommand imports) ──────────────────────────────────────


def test_lazy_group_lists_all_commands_without_importing() -> None:
    """list_commands reports every lazy subcommand name — used by `--help` — but
    never triggers the import (an import would happen in get_command, not here)."""
    group = LazyGroup(
        name="root",
        lazy_subcommands={"does-not-exist": "winter_cli.no.such.module:thing"},
    )
    ctx = click.Context(group)
    # No import error despite the bogus path — list_commands must not import.
    assert group.list_commands(ctx) == ["does-not-exist"]


def test_lazy_group_get_command_imports_on_dispatch() -> None:
    group = LazyGroup(
        name="root",
        lazy_subcommands={"ws": "winter_cli.modules.workspace.command:ws_group"},
    )
    ctx = click.Context(group)
    cmd = group.get_command(ctx, "ws")
    assert isinstance(cmd, click.Group)
    assert cmd.name == "ws"
    # Unknown command falls through to the base implementation (None).
    assert group.get_command(ctx, "nope") is None


def test_cli_group_advertises_every_top_level_command() -> None:
    """`winter --help` must still list all top-level commands."""
    ctx = click.Context(_cli_group)
    assert sorted(_cli_group.list_commands(ctx)) == [
        "capabilities",
        "dashboard",
        "doctor",
        "ext",
        "graph",
        "lint",
        "repo",
        "service",
        "ws",
    ]


def test_cli_lazy_map_targets_resolve() -> None:
    """Every entry in the lazy map points at a real click.Command — guards the
    map against drift if a command is renamed or moved."""
    from winter_cli.cli import _LAZY_SUBCOMMANDS

    group = LazyGroup(name="root", lazy_subcommands=_LAZY_SUBCOMMANDS)
    ctx = click.Context(group)
    for name in _LAZY_SUBCOMMANDS:
        assert isinstance(group.get_command(ctx, name), click.Command)


# ── Bytecode cache redirect ──────────────────────────────────────────────────


def test_bytecode_cache_prefix_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert _bytecode_cache_prefix() == str(Path("/tmp/xdg-cache") / "winter" / "pycache")


def test_bytecode_cache_prefix_defaults_to_home_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/someone")))
    assert _bytecode_cache_prefix() == str(Path("/home/someone") / ".cache" / "winter" / "pycache")


# ── Import-graph guardrails (the lazy-loading payoff) ────────────────────────
#
# Run in a fresh subprocess: `sys.modules` is process-global, so a sibling test
# that imported the tui/doctor trees would poison an in-process assertion. A
# clean interpreter makes "did importing X pull in the heavy trees?" decidable.

_HEAVY_PREFIXES = (
    "winter_cli.modules.tui",
    "winter_cli.modules.doctor",
    "winter_cli.modules.lint",
    "winter_cli.modules.graph",
)


def _heavy_modules_after_importing(target: str) -> list[str]:
    code = (
        f"import {target}, sys\n"
        f"heavy = [m for m in sys.modules if m == 'textual' or m.startswith('textual.')"
        f" or m.startswith({_HEAVY_PREFIXES!r})]\n"
        "print('\\n'.join(sorted(heavy)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def test_importing_cli_does_not_pull_doctor_tui_or_textual() -> None:
    """Importing the CLI entry module must not drag in the doctor / tui (textual)
    / lint trees — they belong only to their own commands."""
    assert _heavy_modules_after_importing("winter_cli.cli") == []


def test_importing_container_does_not_pull_doctor_tui_or_textual() -> None:
    """The DI container is built on every invocation (including the hot
    `winter ws` path), so importing it must not pull the textual / probe trees."""
    assert _heavy_modules_after_importing("winter_cli.container") == []


def test_dont_write_bytecode_is_not_forced_globally() -> None:
    """The old process-wide `sys.dont_write_bytecode = True` is gone — importing
    the CLI must not disable bytecode writing for the whole interpreter."""
    import sys

    # cli_module is imported at the top of this file, so its module body ran.
    assert cli_module is not None
    assert sys.dont_write_bytecode is False
    # Importing the CLI redirects the cache rather than disabling it; the prefix
    # is set process-wide (here, or by a caller who pre-set it before import).
    assert sys.pycache_prefix is not None


# ── Config-error boundary ────────────────────────────────────────────────────
#
# Config-domain errors (ConfigError and ConfigFileReadError) must surface as a
# clean "error: ..." line on stderr with a non-zero exit code — not as a Python
# traceback.  Each test runs in a subprocess so the real cli() entry path is
# exercised end-to-end.


def _run_cli_in_dir(cwd: Path, *extra_args: str, config_toml: str = "") -> subprocess.CompletedProcess:
    """Run `python -m winter_cli.cli ws status` in *cwd*, returning the result."""
    winter_dir = cwd / ".winter"
    winter_dir.mkdir(exist_ok=True)
    if config_toml:
        (winter_dir / "config.toml").write_text(config_toml)
    return subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", "ws", "status", *extra_args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


class TestConfigErrorBoundary:
    def test_outside_workspace_yields_clean_error(self, tmp_path: Path) -> None:
        """Running outside a workspace root prints 'error:' on stderr and exits non-zero."""
        # tmp_path has no .winter/ directory -> workspace locator raises ConfigError.
        result = subprocess.run(
            [sys.executable, "-m", "winter_cli.cli", "ws", "status"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr

    def test_invalid_toml_yields_clean_error(self, tmp_path: Path) -> None:
        """A malformed config.toml prints 'error:' on stderr and exits non-zero."""
        (tmp_path / ".winter").mkdir()
        (tmp_path / ".winter" / "config.toml").write_text("not valid toml ][[\n")
        result = subprocess.run(
            [sys.executable, "-m", "winter_cli.cli", "ws", "status"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr

    def test_out_of_range_envs_per_workspace_yields_clean_error(self, tmp_path: Path) -> None:
        """envs_per_workspace < len(env_aliases)+2 prints 'error:' and exits non-zero."""
        bad_config = dedent(
            """
            main_branch = "main"
            session_prefix = "t"
            env_aliases = ["alpha", "beta", "gamma"]
            envs_per_workspace = 4
            """
        ).strip()
        result = _run_cli_in_dir(tmp_path, config_toml=bad_config)
        assert result.returncode != 0
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
