from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.conftest import ClickRecorder, FakeConfigFileReader, FakeFilesystem, FakeSpecLoader, FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.models import LogOptions
from winter_cli.modules.service.orchestrator_resolver import ResolvedOrchestrator, ServiceOrchestratorResolver
from winter_cli.modules.service.service_logs_service import ServiceLogsService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
EXT = WS / "winter-service-tmux"
ENTRYPOINT = EXT / "workflow/logs"
PREFIX = "winter-service-tmux"

CMD_KEY = f"{ENTRYPOINT} logs alpha"


def _resolved() -> ResolvedOrchestrator:
    return ResolvedOrchestrator(entrypoint=ENTRYPOINT, ext_dir=EXT, prefix=PREFIX)


def _resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve.return_value = _resolved()
    return resolver


def _opts(**kwargs: Any) -> LogOptions:
    defaults: dict[str, Any] = {
        "patterns": ("alpha",),
        "follow": False,
        "tail": 200,
        "since_rfc3339": "",
        "until_rfc3339": "",
        "timestamps": False,
    }
    defaults.update(kwargs)
    return LogOptions(**defaults)


def _make_single_provider_registry(
    runner: FakeSubprocessRunner,
) -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to a single tmux provider."""
    repo = StandaloneRepository(name="winter-service-tmux", path=WS / "winter-service-tmux")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader({repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/logs"}})
    )
    fs = FakeFilesystem(files={repo.path / EXT_MANIFEST: "", repo.path / "workflow/logs": ""})
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        bindings={"service": ["winter-service-tmux"]},
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    resolver = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        fs=fs,
    )
    return registry, resolver


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _svc(
    runner: FakeSubprocessRunner | None = None,
    click: ClickRecorder | None = None,
) -> ServiceLogsService:
    _runner = runner or FakeSubprocessRunner()
    _registry, res = _make_single_provider_registry(_runner)
    describe_svc = ServiceDescribeService(
        subprocess_runner=_runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    return ServiceLogsService(
        subprocess_runner=_runner,
        orchestrator_resolver=res,
        describe_service=describe_svc,
        click=click or ClickRecorder(),
        workspace_root=WS,
    )


# ── WINTER_LOG_* env mapping ──────────────────────────────────────────────────


def test_stream_sets_winter_log_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """WINTER_LOG_* vars are populated from LogOptions before invoking popen."""
    monkeypatch.setenv("WINTER_TEST_CANARY", "canary")
    # patterns are forwarded as argv, not via WINTER_LOG_SERVICES; use a two-pattern
    # key so CMD_KEY reflects the actual command issued.
    multi_cmd_key = f"{ENTRYPOINT} logs alpha/api alpha/db"
    runner = FakeSubprocessRunner(
        popen_responses={multi_cmd_key: (['{"ts":"2026-06-13T10:00:01Z","env":"alpha","svc":"api","msg":"up"}'], 0)}
    )
    _svc(runner).stream(_opts(patterns=("alpha/api", "alpha/db"), follow=False, tail=50, timestamps=True))

    assert len(runner.popen_envs) == 1
    env = runner.popen_envs[0]
    assert env["WINTER_LOG_FOLLOW"] == "0"
    assert env["WINTER_LOG_TAIL"] == "50"
    assert env["WINTER_LOG_SINCE"] == ""
    assert env["WINTER_LOG_UNTIL"] == ""
    assert env["WINTER_LOG_TIMESTAMPS"] == "1"


def test_stream_sets_workspace_context_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, and WINTER_EXT_PREFIX are always injected."""
    monkeypatch.setenv("WINTER_TEST_CANARY", "canary")
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    _svc(runner).stream(_opts())

    env = runner.popen_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(EXT)
    assert env["WINTER_EXT_PREFIX"] == PREFIX


def test_stream_inherits_parent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator env starts from os.environ so inherited vars are preserved."""
    monkeypatch.setenv("WINTER_SENTINEL", "hello")
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    _svc(runner).stream(_opts())

    assert runner.popen_envs[0]["WINTER_SENTINEL"] == "hello"


def test_stream_sets_follow_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTER_TEST_CANARY", "canary")
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    _svc(runner).stream(_opts(follow=True))
    assert runner.popen_envs[0]["WINTER_LOG_FOLLOW"] == "1"


def test_stream_sets_since_until_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTER_TEST_CANARY", "canary")
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    _svc(runner).stream(
        _opts(since_rfc3339="2026-06-13T10:00:00Z", until_rfc3339="2026-06-13T12:00:00Z"),
    )
    env = runner.popen_envs[0]
    assert env["WINTER_LOG_SINCE"] == "2026-06-13T10:00:00Z"
    assert env["WINTER_LOG_UNTIL"] == "2026-06-13T12:00:00Z"


# ── rendered output ───────────────────────────────────────────────────────────


def test_stream_renders_ndjson_lines_to_stdout() -> None:
    """Parsed NDJSON lines are echoed as rendered plain text."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY: (
                [
                    '{"ts":"2026-06-13T10:00:01Z","env":"alpha","svc":"api","msg":"started"}',
                    '{"ts":"2026-06-13T10:00:02Z","env":"alpha","svc":"db","msg":"ready"}',
                ],
                0,
            )
        }
    )
    click = ClickRecorder()
    _svc(runner, click).stream(_opts())
    rendered = [msg for msg, _err in click.calls if not _err]
    assert "alpha/api | started" in rendered
    assert "alpha/db | ready" in rendered


def test_stream_does_not_echo_to_stderr_when_no_warnings() -> None:
    """No stderr output when orchestrator lines all carry timestamps."""
    runner = FakeSubprocessRunner(
        popen_responses={
            CMD_KEY: (
                ['{"ts":"2026-06-13T10:00:01Z","env":"alpha","svc":"api","msg":"up"}'],
                0,
            )
        }
    )
    click = ClickRecorder()
    _svc(runner, click).stream(_opts())
    assert not any(err for _, err in click.calls)


# ── exit code passthrough ─────────────────────────────────────────────────────


def test_stream_passes_exit_code_through() -> None:
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 42)})
    assert _svc(runner).stream(_opts()) == 42


def test_stream_returns_zero_on_clean_exit() -> None:
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    assert _svc(runner).stream(_opts()) == 0


# ── KeyboardInterrupt paths ───────────────────────────────────────────────────


class _InterruptOnIterRunner:
    """ISubprocessRunner that raises KeyboardInterrupt while iterating stdout_lines."""

    def run(self, cmd: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> SubprocessResult:
        raise AssertionError("unexpected run call")

    def call(self, cmd: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        raise AssertionError("unexpected call")

    @contextmanager
    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        env: Any = None,
        shell: bool = False,
        merge_stderr: bool = True,
    ) -> Iterator[Any]:
        yield _InterruptOnIterProcess()


class _InterruptOnIterProcess:
    @property
    def stdout_lines(self) -> Iterator[str]:
        raise KeyboardInterrupt
        yield  # make it a generator

    def wait(self) -> int:
        return 0


class _InterruptOnPopenRunner:
    """ISubprocessRunner that raises KeyboardInterrupt before entering the popen context."""

    def run(self, cmd: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> SubprocessResult:
        raise AssertionError("unexpected run call")

    def call(self, cmd: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        raise AssertionError("unexpected call")

    @contextmanager
    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        env: Any = None,
        shell: bool = False,
        merge_stderr: bool = True,
    ) -> Iterator[Any]:
        raise KeyboardInterrupt
        yield  # type: ignore[misc]  # unreachable — makes this function a generator


def test_stream_returns_130_on_keyboard_interrupt_during_iteration() -> None:
    """KeyboardInterrupt raised while reading stdout_lines returns 130."""
    interrupt_runner = _InterruptOnIterRunner()
    _registry, res = _make_single_provider_registry(FakeSubprocessRunner())
    describe_svc = ServiceDescribeService(
        subprocess_runner=FakeSubprocessRunner(),
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    svc = ServiceLogsService(
        subprocess_runner=interrupt_runner,
        orchestrator_resolver=res,
        describe_service=describe_svc,
        click=ClickRecorder(),
        workspace_root=WS,
    )
    assert svc.stream(_opts()) == 130


def test_stream_returns_130_on_keyboard_interrupt_at_popen() -> None:
    """KeyboardInterrupt raised at popen entry returns 130."""
    interrupt_runner = _InterruptOnPopenRunner()
    _registry, res = _make_single_provider_registry(FakeSubprocessRunner())
    describe_svc = ServiceDescribeService(
        subprocess_runner=FakeSubprocessRunner(),
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    svc = ServiceLogsService(
        subprocess_runner=interrupt_runner,
        orchestrator_resolver=res,
        describe_service=describe_svc,
        click=ClickRecorder(),
        workspace_root=WS,
    )
    assert svc.stream(_opts()) == 130


# ── conditional stderr warnings ───────────────────────────────────────────────


def test_stream_emits_timestamps_warning_when_ts_missing_and_timestamps_requested() -> None:
    """`-t` requested but lines carry no ts field → warning on stderr."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: (['{"env":"alpha","svc":"api","msg":"up"}'], 0)})
    click = ClickRecorder()
    _svc(runner, click).stream(_opts(timestamps=True))

    stderr_msgs = [msg for msg, err in click.calls if err]
    assert any("timestamp prefixes omitted" in m for m in stderr_msgs)


def test_stream_emits_time_filter_warning_when_ts_missing_and_since_set() -> None:
    """--since set but some lines carry no ts → partial filter warning on stderr."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: (['{"env":"alpha","svc":"api","msg":"up"}'], 0)})
    click = ClickRecorder()
    _svc(runner, click).stream(
        _opts(since_rfc3339="2026-06-13T10:00:00Z"),
    )

    stderr_msgs = [msg for msg, err in click.calls if err]
    assert any("--since/--until filter is partial" in m for m in stderr_msgs)


def test_stream_popen_invoked_with_merge_stderr_false() -> None:
    """popen is always called with merge_stderr=False so orchestrator stderr reaches the terminal."""
    runner = FakeSubprocessRunner(popen_responses={CMD_KEY: ([], 0)})
    _svc(runner).stream(_opts())
    assert runner.popen_merge_stderr == [False]


# ── workspace scope forwarding ────────────────────────────────────────────────


def test_stream_workspace_pattern_forwarded_verbatim_on_argv() -> None:
    """'workspace' pattern is forwarded verbatim as a positional argv token."""
    key = f"{ENTRYPOINT} logs workspace"
    runner = FakeSubprocessRunner(popen_responses={key: ([], 0)})
    _svc(runner).stream(_opts(patterns=("workspace",)))
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "logs", "workspace"]


def test_stream_workspace_service_pattern_forwarded_verbatim_on_argv() -> None:
    """'workspace/<svc>' pattern is forwarded verbatim as a positional argv token."""
    key = f"{ENTRYPOINT} logs workspace/nginx"
    runner = FakeSubprocessRunner(popen_responses={key: ([], 0)})
    _svc(runner).stream(_opts(patterns=("workspace/nginx",)))
    assert runner.popen_calls[0][0] == [str(ENTRYPOINT), "logs", "workspace/nginx"]
