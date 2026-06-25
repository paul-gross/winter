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
from winter_cli.config.models import ProjectRepositoryConfig, SingletonRepository, SingletonType, WorkspaceConfig
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
from winter_cli.modules.service.service_readiness_service import ServiceReadinessService
from winter_cli.modules.service.service_reporter import JsonServiceReporter, StreamServiceReporter
from winter_cli.modules.service.service_status_matrix_service import ServiceStatusMatrixService
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


class _FakeEnvFileSourcer:
    def source(self, scope: str, ws_root: Path) -> dict[str, str]:
        return {}


class _FakeEnvIndexRegistry:
    def __init__(self, assignments: dict[str, int]) -> None:
        self._data: dict[str, int] = dict(assignments)

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


def _ws_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WS,
        session_prefix="test",
        main_branch="main",
        base_port=4000,
        ports_per_env=20,
        singleton_repos=[SingletonRepository(name="ws", type=SingletonType.workspace)],
        project_repos=[ProjectRepositoryConfig(name="demo", url="git@example.com:demo.git")],
    )


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
    cli_output = ClickCliOutputService()
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
        workspace_root=WS,
    )
    matrix = ServiceStatusMatrixService(
        subprocess_runner=runner,
        describe_service=describe_svc,
        env_file_sourcer=_FakeEnvFileSourcer(),
        status_parser=StatusDocumentParser(),
        workspace_config=_ws_config(),
        env_index_registry=_FakeEnvIndexRegistry({"alpha": 1}),
        workspace_root=WS,
    )
    status = ServiceStatusService(
        orchestrator_resolver=res,
        status_parser=StatusDocumentParser(),
        matrix_service=matrix,
    )
    readiness = ServiceReadinessService(
        status_service=status,
        sleep=lambda _s: None,
        monotonic=_counting_clock(),
    )
    stream_reporter = StreamServiceReporter(click=click_obj, cli_output=cli_output)
    json_reporter = JsonServiceReporter(click=click_obj, cli_output=cli_output)
    return ServiceHandler(
        dispatch,
        logs,
        status,
        readiness_service=readiness,
        stream_reporter=stream_reporter,
        json_reporter=json_reporter,
    )


def _counting_clock(step: float = 1.0):
    """Return a monotonic() stub that advances by *step* on each call."""
    ticks = iter(range(0, 10**9))

    def _clock() -> float:
        return next(ticks) * step

    return _clock


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
    """Multiple scope-qualified patterns for the same scope expand to <scope>/*.

    When two patterns target the same scope (alpha/web, alpha/api), the matrix
    forwards alpha/* to the provider so neither service is silently dropped.
    The post-merge filter_status backstop then narrows the result to web+api.
    The workspace scope is not included because no pattern targets 'workspace'.
    """
    runner = FakeSubprocessRunner(popen_responses={f"{STATUS_ENTRYPOINT} status alpha/*": ([_SIMPLE_STATUS_DOC], 0)})
    _handler(runner).run_status(StatusOptions(patterns=("alpha/web", "alpha/api"), as_json=False))
    assert len(runner.popen_calls) == 1
    cmd = runner.popen_calls[0][0]
    assert cmd == [STATUS_ENTRYPOINT, "status", "alpha/*"]


def test_handler_status_with_no_patterns_sends_bare_action() -> None:
    """No patterns → full matrix: one cell per configured env + workspace cell.

    The sole provider is called with 'alpha/*' and 'workspace/*' (registry has alpha=1).
    """
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{STATUS_ENTRYPOINT} status alpha/*": ([_SIMPLE_STATUS_DOC], 0),
            f"{STATUS_ENTRYPOINT} status workspace/*": (
                [
                    json.dumps({"envs": []}),
                ],
                0,
            ),
        }
    )
    _handler(runner).run_status(StatusOptions(patterns=(), as_json=False))
    # Two cells: alpha/* and workspace/*, both invoked.
    assert len(runner.popen_calls) == 2
    cmds = [calls[0] for calls in runner.popen_calls]
    assert any(cmd == [STATUS_ENTRYPOINT, "status", "alpha/*"] for cmd in cmds)
    assert any(cmd == [STATUS_ENTRYPOINT, "status", "workspace/*"] for cmd in cmds)


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
    """run_status exits with the orchestrator's non-zero exit code regardless of stdout validity.

    Both matrix cells are called; workspace/* returns an empty valid doc; alpha/* returns
    invalid JSON with exit code 3.  The worst exit code (3) is propagated.
    """
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{STATUS_ENTRYPOINT} status alpha/*": (["not valid json"], 3),
            f"{STATUS_ENTRYPOINT} status workspace/*": (
                [
                    json.dumps({"envs": []}),
                ],
                0,
            ),
        }
    )
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner).run_status(StatusOptions(patterns=(), as_json=False))
    assert excinfo.value.code == 3


def test_handler_up_exits_zero_without_raising() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))


def test_handler_status_exits_zero_without_raising() -> None:
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{STATUS_ENTRYPOINT} status alpha/*": ([_SIMPLE_STATUS_DOC], 0),
            f"{STATUS_ENTRYPOINT} status workspace/*": (
                [
                    json.dumps({"envs": []}),
                ],
                0,
            ),
        }
    )
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
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api --tail 200": (ndjson_lines, 0)})
    recorder = ClickRecorder()
    _handler(runner, recorder).run_logs(_default_log_options(patterns=("alpha/api",)))
    # Both lines rendered (single pattern → no prefix).
    assert any("started" in m for m, _ in recorder.calls)
    assert any("ready" in m for m, _ in recorder.calls)


def test_handler_run_logs_exits_nonzero_on_orchestrator_error() -> None:
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api --tail 200": ([], 2)})
    with pytest.raises(SystemExit) as excinfo:
        _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api",)))
    assert excinfo.value.code == 2


def test_handler_run_logs_sets_workspace_context_env_vars() -> None:
    """Logs stream injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, and cwd."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api --tail 200": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api",)))
    assert len(runner.popen_calls) == 1
    assert runner.popen_calls[0][1] == WS
    assert len(runner.popen_envs) == 1
    env = runner.popen_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"


def test_handler_run_logs_patterns_on_argv_not_env_var() -> None:
    """Patterns appear as positional argv tokens (before render flags); WINTER_LOG_SERVICES is absent."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs alpha/api beta/worker-* --tail 200": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=("alpha/api", "beta/worker-*")))
    assert len(runner.popen_calls) == 1
    cmd = runner.popen_calls[0][0]
    assert cmd == [ENTRYPOINT, "logs", "alpha/api", "beta/worker-*", "--tail", "200"]
    env = runner.popen_envs[0]
    assert "WINTER_LOG_SERVICES" not in env


def test_handler_run_logs_no_patterns_sends_bare_logs_action() -> None:
    """Empty patterns → argv carries no selection tokens, only the render flags."""
    runner = FakeSubprocessRunner(popen_responses={f"{ENTRYPOINT} logs --tail 200": ([], 0)})
    _handler(runner, ClickRecorder()).run_logs(_default_log_options(patterns=()))
    cmd = runner.popen_calls[0][0]
    assert cmd == [ENTRYPOINT, "logs", "--tail", "200"]
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


# ── up --wait readiness gate ────────────────────────────────────────────────


def _status_doc(env: str, services: list[tuple[str, str]]) -> str:
    """Serialise a minimal status document: services is a list of (name, health)."""
    return json.dumps(
        {
            "envs": [
                {
                    "env": env,
                    "session": f"mp-{env}",
                    "port_base": 4020,
                    "services": [
                        {
                            "name": name,
                            "state": "running",
                            "health": health,
                            "ports": [],
                            "handle": None,
                            "log_path": None,
                            "since": None,
                        }
                        for name, health in services
                    ],
                }
            ]
        }
    )


def test_handler_up_wait_exits_zero_when_healthy() -> None:
    """Readiness poll uses collect("alpha") → matrix cell alpha/* for the sole provider."""
    runner = FakeSubprocessRunner(
        popen_responses={f"{STATUS_ENTRYPOINT} status alpha/*": ([_status_doc("alpha", [("api", "healthy")])], 0)},
    )
    # No SystemExit: ready before timeout → success.
    _handler(runner).run(ServiceParams(action="up", env="alpha", wait=True, timeout_s=30.0))
    # up was dispatched (workspace + env), then status was polled once.
    assert runner.call_calls == [
        ([ENTRYPOINT, "up", "workspace"], WS),
        ([ENTRYPOINT, "up", "alpha"], WS),
    ]
    assert runner.popen_calls == [([STATUS_ENTRYPOINT, "status", "alpha/*"], WS)]


def test_handler_up_wait_returns_promptly_with_no_declared_probes() -> None:
    # Every service reports "unknown" (no probe) → no blocking, exit 0 on first poll.
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{STATUS_ENTRYPOINT} status alpha/*": (
                [_status_doc("alpha", [("api", "unknown"), ("web", "unknown")])],
                0,
            )
        },
    )
    _handler(runner).run(ServiceParams(action="up", env="alpha", wait=True, timeout_s=30.0))
    assert runner.popen_calls == [([STATUS_ENTRYPOINT, "status", "alpha/*"], WS)]


def test_handler_up_wait_times_out_and_names_unhealthy_services() -> None:
    click = ClickRecorder()
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{STATUS_ENTRYPOINT} status alpha/*": (
                [_status_doc("alpha", [("api", "unhealthy"), ("web", "healthy")])],
                0,
            )
        },
    )
    with pytest.raises(SystemExit) as excinfo:
        # Small timeout: the counting clock crosses the deadline after the first poll.
        _handler(runner, click=click).run(ServiceParams(action="up", env="alpha", wait=True, timeout_s=0.5))
    assert excinfo.value.code == 1
    # The still-unhealthy service is named on stderr.
    stderr = [msg for msg, err in click.calls if err]
    assert any("alpha/api" in msg for msg in stderr)
    assert any("unhealthy" in msg for msg in stderr)


def test_handler_up_without_wait_does_not_poll_status() -> None:
    runner = FakeSubprocessRunner()
    _handler(runner).run(ServiceParams(action="up", env="alpha"))
    # No --wait → up behaves exactly as before: no status poll.
    assert runner.popen_calls == []
