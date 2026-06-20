from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    ClickRecorder,
    FakeConfigFileReader,
    FakeFilesystem,
    FakeSpecLoader,
    FakeSubprocessRunner,
)
from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.handler import ServiceHandler, ServiceParams
from winter_cli.modules.service.models import LogOptions
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService
from winter_cli.modules.service.service_logs_service import ServiceLogsService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_status_service import ServiceStatusService
from winter_cli.modules.service.status_models import StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
ENTRYPOINT = str(WS / "winter-service-tmux/workflow/service")
STATUS_ENTRYPOINT = str(WS / "winter-service-tmux/workflow/service")

_SIMPLE_STATUS_DOC = json.dumps(
    {
        "envs": [
            {
                "env": "alpha",
                "session": "mp-alpha",
                "port_base": 4020,
                "services": [
                    {
                        "name": "api",
                        "state": "running",
                        "health": "healthy",
                        "ports": [7503],
                        "handle": None,
                        "log_path": None,
                        "since": None,
                    }
                ],
            }
        ]
    }
)


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _make_registry_and_resolver(
    runner: FakeSubprocessRunner,
) -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    repo = StandaloneRepository(name="winter-service-tmux", path=WS / "winter-service-tmux")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}}
        )
    )
    fs = FakeFilesystem(files={repo.path / EXT_MANIFEST: "", repo.path / "workflow/service": ""})
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


def _handler(runner: FakeSubprocessRunner, click: Any = None) -> ServiceHandler:
    _registry, res = _make_registry_and_resolver(runner)
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    click_obj = click or ClickRecorder()
    fan_out = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
    )
    dispatch = ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=res,
        fan_out_service=fan_out,
        describe_service=describe_svc,
        workspace_root=WS,
    )
    logs = ServiceLogsService(
        subprocess_runner=runner,
        orchestrator_resolver=res,
        describe_service=describe_svc,
        click=click_obj,
        workspace_root=WS,
    )
    status = ServiceStatusService(
        subprocess_runner=runner,
        orchestrator_resolver=res,
        status_parser=StatusDocumentParser(),
        cli_output=ClickCliOutputService(),
        click=click_obj,
        workspace_root=WS,
    )
    return ServiceHandler(dispatch, logs, status)


# ── dispatch actions ──────────────────────────────────────────────────────────


def test_handler_up_invokes_entrypoint_with_action_and_env() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))
    # workspace-ensure dispatch precedes the env dispatch.
    assert runner.call_calls == [
        ([ENTRYPOINT, "up", "workspace"], WS),
        ([ENTRYPOINT, "up", "alpha"], WS),
    ]


def test_handler_down_invokes_correct_argv() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="down", env="beta"))
    assert runner.call_calls == [([ENTRYPOINT, "down", "beta"], WS)]


def test_handler_up_does_not_set_selection_env_vars() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))
    # Two dispatches (workspace-ensure + env); neither sets selection env vars.
    assert len(runner.call_envs) == 2
    for env in runner.call_envs:
        assert "WINTER_SERVICE_NAME" not in env
        assert "WINTER_SERVICE_PATTERNS" not in env


def test_handler_down_does_not_set_selection_env_vars() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="down", env="alpha"))
    assert len(runner.call_envs) == 1
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_NAME" not in env
    assert "WINTER_SERVICE_PATTERNS" not in env


def test_handler_restart_with_patterns_forwards_them_on_argv() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="restart", patterns=("alpha/api", "*/backend")))
    assert runner.call_calls == [([ENTRYPOINT, "restart", "alpha/api", "*/backend"], WS)]
    assert len(runner.call_envs) == 1
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_NAME" not in env
    assert "WINTER_SERVICE_PATTERNS" not in env
    assert "PATH" in env
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"


def test_handler_restart_workspace_pattern_forwarded_verbatim() -> None:
    """restart workspace → argv is [entrypoint, 'restart', 'workspace'] verbatim."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="restart", patterns=("workspace",)))
    assert runner.call_calls == [([ENTRYPOINT, "restart", "workspace"], WS)]


def test_handler_restart_workspace_service_pattern_forwarded_verbatim() -> None:
    """restart workspace/<svc> → argv forwards the compound pattern unchanged."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="restart", patterns=("workspace/nginx",)))
    assert runner.call_calls == [([ENTRYPOINT, "restart", "workspace/nginx"], WS)]


def test_handler_status_with_patterns_forwards_them_on_argv() -> None:
    runner = FakeSubprocessRunner(
        popen_responses={f"{STATUS_ENTRYPOINT} status alpha/web alpha/api": ([_SIMPLE_STATUS_DOC], 0)}
    )
    _handler(runner).run_status(StatusOptions(patterns=("alpha/web", "alpha/api"), as_json=False))
    assert len(runner.popen_calls) == 1
    cmd = runner.popen_calls[0][0]
    assert cmd == [STATUS_ENTRYPOINT, "status", "alpha/web", "alpha/api"]


def test_handler_status_with_no_patterns_sends_bare_action() -> None:
    runner = FakeSubprocessRunner(popen_responses={f"{STATUS_ENTRYPOINT} status": ([_SIMPLE_STATUS_DOC], 0)})
    _handler(runner).run_status(StatusOptions(patterns=(), as_json=False))
    assert len(runner.popen_calls) == 1
    cmd = runner.popen_calls[0][0]
    assert cmd == [STATUS_ENTRYPOINT, "status"]


def test_handler_adopts_nonzero_exit_code() -> None:
    runner = FakeSubprocessRunner(call_responses={f"{ENTRYPOINT} restart alpha/api": 7})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run(ServiceParams(action="restart", patterns=("alpha/api",)))
    assert excinfo.value.code == 7


def test_handler_adopts_nonzero_exit_code_for_restart() -> None:
    runner = FakeSubprocessRunner(call_responses={f"{ENTRYPOINT} restart alpha/api": 5})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run(ServiceParams(action="restart", patterns=("alpha/api",)))
    assert excinfo.value.code == 5


def test_handler_run_status_adopts_nonzero_exit_code() -> None:
    """run_status exits with the orchestrator's non-zero exit code regardless of stdout validity."""
    runner = FakeSubprocessRunner(popen_responses={f"{STATUS_ENTRYPOINT} status": (["not valid json"], 3)})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run_status(StatusOptions(patterns=(), as_json=False))
    assert excinfo.value.code == 3


def test_handler_up_exits_zero_without_raising() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))


def test_handler_status_exits_zero_without_raising() -> None:
    runner = FakeSubprocessRunner(popen_responses={f"{STATUS_ENTRYPOINT} status": ([_SIMPLE_STATUS_DOC], 0)})
    _handler(runner).run_status(StatusOptions(patterns=(), as_json=False))


# ── logs via run_logs ─────────────────────────────────────────────────────────


def _default_log_options(**kwargs: Any) -> LogOptions:
    defaults: dict[str, Any] = {
        "patterns": (),
        "follow": False,
        "tail": 200,
        "since_rfc3339": "",
        "until_rfc3339": "",
        "timestamps": False,
    }
    defaults.update(kwargs)
    return LogOptions(**defaults)


def test_handler_run_logs_streams_rendered_output() -> None:
    ndjson_lines = [
        '{"env":"alpha","ts":"2026-06-13T10:00:01Z","svc":"api","msg":"started"}',
        '{"env":"alpha","ts":"2026-06-13T10:00:02Z","svc":"api","msg":"ready"}',
    ]
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api": (ndjson_lines, 0)})
    recorder = ClickRecorder()
    _handler(runner, recorder).run_logs(_default_log_options(patterns=("alpha/api",)))
    # Both lines rendered (single pattern → no prefix).
    assert any("started" in m for m, _ in recorder.calls)
    assert any("ready" in m for m, _ in recorder.calls)


def test_handler_run_logs_exits_nonzero_on_orchestrator_error() -> None:
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api": ([], 2)})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api",)))
    assert excinfo.value.code == 2


def test_handler_run_logs_sets_workspace_context_env_vars() -> None:
    """Logs stream injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, and cwd."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api",)))
    assert len(runner.popen_calls) == 1
    assert runner.popen_calls[0][1] == WS
    assert len(runner.popen_envs) == 1
    env = runner.popen_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"


def test_handler_run_logs_patterns_on_argv_not_env_var() -> None:
    """Patterns appear as positional argv tokens; WINTER_LOG_SERVICES is absent."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api beta/worker-*": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api", "beta/worker-*")))
    assert len(runner.popen_calls) == 1
    cmd = runner.popen_calls[0][0]
    assert cmd == [ENTRYPOINT, "logs", "alpha/api", "beta/worker-*"]
    env = runner.popen_envs[0]
    assert "WINTER_LOG_SERVICES" not in env


def test_handler_run_logs_no_patterns_sends_bare_logs_action() -> None:
    """Empty patterns → argv is just [entrypoint, 'logs']; no selection tokens."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=()))
    cmd = runner.popen_calls[0][0]
    assert cmd == [ENTRYPOINT, "logs"]
    env = runner.popen_envs[0]
    assert "WINTER_LOG_SERVICES" not in env


# ── workspace-scope lifecycle ─────────────────────────────────────────────────


def test_handler_up_workspace_target_single_dispatch() -> None:
    """up workspace → exactly one dispatch: up workspace. No recursion."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="workspace"))
    assert runner.call_calls == [([ENTRYPOINT, "up", "workspace"], WS)]


def test_handler_down_workspace_target_single_dispatch() -> None:
    """down workspace → exactly one dispatch: down workspace."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="down", env="workspace"))
    assert runner.call_calls == [([ENTRYPOINT, "down", "workspace"], WS)]


def test_handler_up_env_ensures_workspace_first() -> None:
    """up alpha → dispatch workspace first, then alpha (workspace-ensure ordering)."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))
    assert runner.call_calls == [
        ([ENTRYPOINT, "up", "workspace"], WS),
        ([ENTRYPOINT, "up", "alpha"], WS),
    ]


def test_handler_down_env_leaves_workspace_running() -> None:
    """down alpha → only down alpha; no down workspace call."""
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="down", env="alpha"))
    assert runner.call_calls == [([ENTRYPOINT, "down", "alpha"], WS)]


def test_handler_up_ensure_failure_is_best_effort() -> None:
    """up workspace failure does NOT skip up alpha — both are dispatched (best-effort).

    The overall exit code is the workspace failure code (first non-zero seen).
    """
    runner = FakeSubprocessRunner(call_responses={f"{ENTRYPOINT} up workspace": 4})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run(ServiceParams(action="up", env="alpha"))
    # Both calls were made — workspace first, then env (best-effort: never skip).
    assert runner.call_calls == [
        ([ENTRYPOINT, "up", "workspace"], WS),
        ([ENTRYPOINT, "up", "alpha"], WS),
    ]
    assert excinfo.value.code == 4


def test_handler_up_env_failure_exits_with_env_code() -> None:
    """If workspace up succeeds (0) but env up fails, exit with the env failure code."""
    runner = FakeSubprocessRunner(call_responses={f"{ENTRYPOINT} up alpha": 5})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run(ServiceParams(action="up", env="alpha"))
    assert runner.call_calls == [
        ([ENTRYPOINT, "up", "workspace"], WS),
        ([ENTRYPOINT, "up", "alpha"], WS),
    ]
    assert excinfo.value.code == 5
