from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import click
import pytest

from winter_cli import cli as cli_module
from winter_cli.cli import LazyGroup, _bytecode_cache_prefix, _cli_group, _configure_logging

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
        "clean",
        "dashboard",
        "doctor",
        "env",
        "ext",
        "graph",
        "lint",
        "provision",
        "repo",
        "service",
        "space",
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


# ── Logging configuration ─────────────────────────────────────────────────────


class TestConfigureLogging:
    """Unit tests for _configure_logging — isolate by resetting the logger after each test."""

    def setup_method(self) -> None:
        """Reset the winter_cli logger to a clean state before each test."""
        self._logger = logging.getLogger("winter_cli")
        self._orig_level = self._logger.level
        self._orig_handlers = self._logger.handlers[:]
        self._logger.handlers.clear()
        self._logger.setLevel(logging.NOTSET)

    def teardown_method(self) -> None:
        """Restore logger state after each test."""
        self._logger.handlers.clear()
        for h in self._orig_handlers:
            self._logger.addHandler(h)
        self._logger.setLevel(self._orig_level)

    def test_verbose_flag_attaches_debug_handler_on_stderr(self) -> None:
        """--verbose / -v wires a stderr StreamHandler at DEBUG."""
        _configure_logging(verbose=True, log_level_env=None)
        assert len(self._logger.handlers) == 1
        handler = self._logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
        assert self._logger.level == logging.DEBUG

    def test_env_var_info_attaches_info_handler(self) -> None:
        """WINTER_LOG_LEVEL=INFO wires a StreamHandler at INFO."""
        _configure_logging(verbose=False, log_level_env="INFO")
        assert len(self._logger.handlers) == 1
        assert self._logger.level == logging.INFO

    def test_env_var_warning_attaches_warning_handler(self) -> None:
        """WINTER_LOG_LEVEL=WARNING wires a StreamHandler at WARNING."""
        _configure_logging(verbose=False, log_level_env="WARNING")
        assert len(self._logger.handlers) == 1
        assert self._logger.level == logging.WARNING

    def test_env_var_case_insensitive(self) -> None:
        """WINTER_LOG_LEVEL is case-insensitive (e.g. 'debug' works)."""
        _configure_logging(verbose=False, log_level_env="debug")
        assert len(self._logger.handlers) == 1
        assert self._logger.level == logging.DEBUG

    def test_verbose_takes_precedence_over_env_var(self) -> None:
        """--verbose always sets DEBUG even when WINTER_LOG_LEVEL names a higher level."""
        _configure_logging(verbose=True, log_level_env="WARNING")
        assert self._logger.level == logging.DEBUG

    def test_neither_flag_nor_env_attaches_no_handler(self) -> None:
        """Without --verbose and without WINTER_LOG_LEVEL, no handler is added (silent)."""
        _configure_logging(verbose=False, log_level_env=None)
        assert len(self._logger.handlers) == 0

    def test_unknown_env_var_level_attaches_no_handler(self) -> None:
        """An unrecognised WINTER_LOG_LEVEL value is silently ignored — no handler, no crash."""
        _configure_logging(verbose=False, log_level_env="BOGUS_LEVEL")
        assert len(self._logger.handlers) == 0

    def test_env_var_empty_string_attaches_no_handler(self) -> None:
        """An empty WINTER_LOG_LEVEL (set but blank) is treated as not set."""
        _configure_logging(verbose=False, log_level_env="")
        assert len(self._logger.handlers) == 0


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
            service_prefix = "t"
            env_aliases = ["alpha", "beta", "gamma"]
            envs_per_workspace = 4
            """
        ).strip()
        result = _run_cli_in_dir(tmp_path, config_toml=bad_config)
        assert result.returncode != 0
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr


# ── ctx.exit(n) propagation ───────────────────────────────────────────────────
#
# `_cli_group.main(standalone_mode=False)` does not exit the process on an
# in-command `ctx.exit(n)` — Click raises `Exit(n)` internally and `main()`
# hands the exit code back to its caller instead of calling `sys.exit`
# (see `click.core.BaseCommand.main`). `cli()` must forward that return value
# to `sys.exit` itself. Each test below runs the real `python -m
# winter_cli.cli` entry point in a subprocess, so it pins the *process* exit
# status rather than a command function's return value — a subprocess-less
# test that only asserted the click command's own exit code (e.g. via
# `CliRunner`, which manages `standalone_mode` itself) would not catch a
# regression here.


def _run_winter(cwd: Path, *args: str, config_toml: str = 'main_branch = "master"\n') -> subprocess.CompletedProcess:
    """Run `python -m winter_cli.cli <args>` in a minimal workspace at *cwd*."""
    winter_dir = cwd / ".winter"
    winter_dir.mkdir(exist_ok=True)
    (winter_dir / "config.toml").write_text(config_toml)
    return subprocess.run(
        [sys.executable, "-m", "winter_cli.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


class TestCtxExitPropagation:
    def test_env_unknown_scope_exits_1_at_process_level(self, tmp_path: Path) -> None:
        """`winter env <unregistered>` calls `ctx.exit(1)` — the process must exit 1."""
        result = _run_winter(tmp_path, "env", "nosuchenv")
        assert result.returncode == 1
        assert "unknown scope" in result.stderr

    def test_env_workspace_scope_still_exits_0(self, tmp_path: Path) -> None:
        """The success path (no `ctx.exit` call) is unaffected — still exits 0."""
        result = _run_winter(tmp_path, "env", "workspace")
        assert result.returncode == 0
        assert "export WINTER_ENV=workspace" in result.stdout

    def test_space_invalid_kind_exits_1_at_process_level(self, tmp_path: Path) -> None:
        """`winter space <bad-kind>` calls `ctx.exit(1)` — the process must exit 1."""
        result = _run_winter(tmp_path, "space", "bad/kind")
        assert result.returncode == 1
        assert "invalid kind" in result.stderr

    def test_space_valid_kind_still_exits_0(self, tmp_path: Path) -> None:
        """The success path (no `ctx.exit` call) is unaffected — still exits 0."""
        result = _run_winter(tmp_path, "space", "scores")
        assert result.returncode == 0
        assert result.stdout.strip().endswith("scores")


# ── `winter env --resolve` gate, end to end ──────────────────────────────────
#
# `winter env` is pure and offline by default: a command entry masks to a
# placeholder and is never run. `--resolve` runs it for real. These run the
# real subprocess-backed CLI entry point (not a fake ICommandEntryRunner) so a
# failing command's RepoError is proven to actually reach cli()'s boundary
# arm rather than a test double's stand-in for it.


class TestEnvResolveGate:
    def test_without_resolve_masks_command_entry(self, tmp_path: Path) -> None:
        """Without --resolve, a command entry prints the placeholder and never runs."""
        config_toml = dedent(
            """
            main_branch = "master"
            [env.workspace.vars]
            SECRET = { command = "echo hunter2" }
            """
        ).strip()
        result = _run_winter(tmp_path, "env", "workspace", config_toml=config_toml)
        assert result.returncode == 0
        assert "export SECRET='<unresolved:command>'" in result.stdout
        assert "hunter2" not in result.stdout

    def test_without_resolve_the_command_process_never_starts(self, tmp_path: Path) -> None:
        """Masking is not just output-shaping: the command's process is genuinely never spawned.

        The command writes a marker file as a side effect, so its absence after
        the run proves non-execution — asserting only that the value is absent
        from stdout would still pass if winter ran the command and discarded
        its output.
        """
        marker = tmp_path / "ran.marker"
        config_toml = dedent(
            f"""
            main_branch = "master"
            [env.workspace.vars]
            SECRET = {{ command = "sh -c 'touch {marker}; echo hunter2'" }}
            """
        ).strip()

        masked = _run_winter(tmp_path, "env", "workspace", config_toml=config_toml)
        assert masked.returncode == 0
        assert "export SECRET='<unresolved:command>'" in masked.stdout
        assert not marker.exists()

        # The same command under --resolve does run it — so the assertion above
        # is about the gate, not about an inert command.
        resolved = _run_winter(tmp_path, "env", "workspace", "--resolve", config_toml=config_toml)
        assert resolved.returncode == 0
        assert "export SECRET=hunter2" in resolved.stdout
        assert marker.exists()

    def test_resolve_flag_runs_the_command_and_prints_its_value(self, tmp_path: Path) -> None:
        """--resolve runs the command entry and prints its real output."""
        config_toml = dedent(
            """
            main_branch = "master"
            [env.workspace.vars]
            SECRET = { command = "echo hunter2" }
            """
        ).strip()
        result = _run_winter(tmp_path, "env", "workspace", "--resolve", config_toml=config_toml)
        assert result.returncode == 0
        assert "export SECRET=hunter2" in result.stdout

    def test_resolve_flag_failing_command_exits_1_with_clean_error(self, tmp_path: Path) -> None:
        """A command entry that exits non-zero under --resolve reaches the CLI boundary as 'error: ...' exit 1."""
        config_toml = dedent(
            """
            main_branch = "master"
            [env.workspace.vars]
            SECRET = { command = "false" }
            """
        ).strip()
        result = _run_winter(tmp_path, "env", "workspace", "--resolve", config_toml=config_toml)
        assert result.returncode == 1
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
        assert not any(line.startswith("export ") for line in result.stdout.splitlines())
