"""`winter.env` through the real click tree, driven with a fake tracer.

Each test runs a real command through `_cli_group` with a `Container` whose tracer is a
recorder and whose handlers are stubs, so what is asserted is exactly the span annotation the
command's own arguments produce.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner, Result
from dependency_injector import providers

from tests.conftest import FakeEnvIndexRegistry, FakeSubprocessRunner
from tests.modules.service.test_service_dispatch_service import _dispatch_svc, _single_provider
from winter_cli.cli import _cli_group
from winter_cli.cli_context import CliContext
from winter_cli.container import Container
from winter_cli.core.tracing import TracingSettings
from winter_cli.modules.provision.handler import ProvisionCommandHandler
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService

_TRACING_ON = TracingSettings(otlp_endpoint="http://localhost:4318")


class _RecordingTracer:
    """Records the span annotations; implements the whole `ICommandTracer` surface."""

    def __init__(self) -> None:
        self.envs: list[str] = []

    def inject(self, env: dict[str, str]) -> None:
        pass

    def annotate_env(self, env_name: str) -> None:
        self.envs.append(env_name)

    def start_command(self, command_path: str) -> None:
        pass

    def end_command(self, error_type: str | None) -> None:
        pass

    def export(self) -> None:
        pass


class _Discovery:
    """Stands in for `EnvNameDiscovery`: a fixed list of envs, or a failure."""

    def __init__(self, names: list[str], error: Exception | None = None) -> None:
        self._names = names
        self._error = error
        self.runs = 0

    def names(self) -> list[str]:
        self.runs += 1
        if self._error is not None:
            raise self._error
        return list(self._names)


class _EmptyEnvProvisioner:
    def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _outside_any_workspace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every command from an empty directory, so an unstubbed path cannot reach a live workspace."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WINTER_INVOCATION_CWD", str(tmp_path))


def _container(
    tracer: _RecordingTracer,
    *,
    discovery: _Discovery | None = None,
    settings: TracingSettings = _TRACING_ON,
    **handlers: Any,
) -> Container:
    stubs: dict[str, Any] = {
        "workspace_handler": MagicMock(),
        "init_handler": MagicMock(),
        "destroy_handler": MagicMock(),
        "restack_handler": MagicMock(),
        "provision_command_handler": MagicMock(),
        "service_handler": MagicMock(),
        "lint_handler": MagicMock(),
        "repo_handler": MagicMock(),
        **handlers,
    }

    container = Container()
    container.command_tracer.override(providers.Object(tracer))
    container.tracing_settings.override(providers.Object(settings))
    container.env_name_discovery.override(providers.Object(discovery or _Discovery(["alpha", "beta"])))
    container.env_index_registry.override(providers.Object(FakeEnvIndexRegistry({"alpha": 1, "beta": 2})))
    container.env_provisioner.override(providers.Object(_EmptyEnvProvisioner()))
    for name, stub in stubs.items():
        getattr(container, name).override(providers.Object(stub))
    return container


def _invoke(container: Container, *args: str) -> Result:
    return CliRunner().invoke(_cli_group, list(args), obj=CliContext.of(container))


def _envs_for(*args: str, discovery: _Discovery | None = None) -> list[str]:
    tracer = _RecordingTracer()
    _invoke(_container(tracer, discovery=discovery), *args)
    return tracer.envs


# ── the invoked command's own env targets ────────────────────────────────────


def test_ws_status_with_one_env_sets_winter_env() -> None:
    assert _envs_for("ws", "status", "alpha") == ["alpha"]


def test_ws_status_with_two_envs_sets_none() -> None:
    assert _envs_for("ws", "status", "alpha", "beta") == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["ws", "status"], []),
        (["ws", "status", "alpha/winter"], ["alpha"]),
        (["ws", "status", "al*"], ["alpha"]),
        (["ws", "status", "*"], []),
        (["ws", "status", "*/winter"], []),
        (["ws", "init", "alpha"], ["alpha"]),
        (["ws", "init", "brand-new"], ["brand-new"]),
        (["ws", "init"], []),
        (["ws", "init", "workspace"], []),
        (["ws", "destroy", "alpha"], ["alpha"]),
        (["ws", "disconnect", "alpha", "beta"], []),
        (["ws", "connect", "alpha", "feature/x"], ["alpha"]),
        (["ws", "connect", "alpha", "beta", "feature/x"], []),
        (["ws", "reset", "alpha/winter", "HEAD~1"], ["alpha"]),
        (["ws", "restack", "alpha", "main"], ["alpha"]),
        (["ws", "restack", "alpha", "beta", "main"], []),
        (["ws", "checkout", "alpha", "feature/x"], ["alpha"]),
        (["ws", "clean", "alpha"], ["alpha"]),
        (["ws", "fetch", "alpha"], ["alpha"]),
        (["ws", "pull", "alpha"], ["alpha"]),
        (["ws", "push", "alpha"], ["alpha"]),
        (["ws", "diff", "alpha"], ["alpha"]),
        (["ws", "merge", "main", "alpha"], ["alpha"]),
        (["ws", "index", "alpha"], ["alpha"]),
        (["ws", "update", "alpha"], []),
        (["provision", "alpha"], ["alpha"]),
        (["provision", "alpha", "beta"], []),
        (["provision", "al*"], ["alpha"]),
        (["clean", "alpha"], ["alpha"]),
        (["env", "alpha"], ["alpha"]),
        (["env", "workspace"], []),
        (["service", "up", "alpha"], ["alpha"]),
        (["service", "up", "alpha", "workspace"], ["alpha"]),
        (["service", "up", "workspace"], []),
        (["service", "start", "alpha/api"], ["alpha"]),
        (["service", "down", "alpha", "beta"], []),
        (["service", "status"], []),
        (["service", "status", "alpha"], ["alpha"]),
        (["service", "restart", "alpha/api"], ["alpha"]),
        (["service", "logs", "alpha/api"], ["alpha"]),
        (["lint"], []),
        (["lint", "alpha"], ["alpha"]),
        (["lint", "winter"], []),
        (["lint", "alpha", "winter"], ["alpha"]),
        (["lint", "al*"], ["alpha"]),
        (["lint", "alpha", "beta"], []),
    ],
)
def test_the_env_a_commands_own_arguments_resolve_to(args: list[str], expected: list[str]) -> None:
    assert _envs_for(*args) == expected


def test_a_value_that_is_not_an_env_token_never_reports() -> None:
    assert _envs_for("repo", "add", "https://example.invalid/alpha.git") == []
    assert _envs_for("repo", "remove", "alpha") == []


# ── nested resolution never sets it ──────────────────────────────────────────


class _AutoStartingProvisionService:
    """Provision's per-env step: the service check auto-starts the env's services, as the real one does."""

    def __init__(self, dispatch: ServiceDispatchService) -> None:
        self._dispatch = dispatch
        self.started: list[str] = []

    def run(self, env_name: str, *args: Any, **kwargs: Any) -> Any:
        assert self._dispatch.dispatch("up", [env_name]) == 0
        self.started.append(env_name)
        return SimpleNamespace(status="ok", exit_code=0)


def _provision_with_autostart(tracer: _RecordingTracer, *args: str) -> list[str]:
    runner = FakeSubprocessRunner()
    provision = _AutoStartingProvisionService(_dispatch_svc(runner, [_single_provider()]))
    handler = ProvisionCommandHandler(
        provision_service=provision,  # type: ignore[arg-type]
        stream_reporter=MagicMock(),
        json_reporter=MagicMock(),
        workspace_repo=MagicMock(),
        repo_factory=MagicMock(),
        workspace=MagicMock(),
    )

    result = _invoke(_container(tracer, provision_command_handler=handler), "provision", *args)

    assert result.exit_code == 0, result.output
    # The service check really did resolve each env and start its services.
    assert [cmd[1:] for cmd, _cwd in runner.call_calls] == [["up", env] for env in provision.started]
    return provision.started


def test_provision_of_two_envs_sets_none_though_each_env_auto_starts_its_services() -> None:
    tracer = _RecordingTracer()

    assert _provision_with_autostart(tracer, "alpha", "beta") == ["alpha", "beta"]
    assert tracer.envs == []


def test_provision_of_one_env_sets_that_env_though_it_auto_starts_its_services() -> None:
    tracer = _RecordingTracer()

    assert _provision_with_autostart(tracer, "alpha") == ["alpha"]
    assert tracer.envs == ["alpha"]


# ── tracing off, and failures, change nothing ────────────────────────────────


def test_with_tracing_off_nothing_is_reported_and_no_discovery_runs() -> None:
    tracer = _RecordingTracer()
    discovery = _Discovery(["alpha", "beta"])
    container = _container(tracer, discovery=discovery, settings=TracingSettings())

    _invoke(container, "ws", "status", "alpha")
    _invoke(container, "ws", "status", "al*")
    _invoke(container, "lint", "alpha")

    assert tracer.envs == []
    assert discovery.runs == 0


def _failing_handler() -> MagicMock:
    handler = MagicMock()
    handler.status.side_effect = click.ClickException("handler said no")
    handler.run.side_effect = click.ClickException("handler said no")
    return handler


def _outcome(result: Result) -> tuple[int, str, str]:
    return result.exit_code, result.stderr, result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["ws", "status", "al*"],
        ["ws", "status", "alpha", "be*"],
        ["provision", "al*"],
        ["lint", "alpha"],
    ],
)
def test_a_discovery_failure_gives_the_same_exit_code_and_output_with_tracing_on_and_off(args: list[str]) -> None:
    def run(settings: TracingSettings, discovery: _Discovery) -> tuple[tuple[int, str, str], list[str]]:
        tracer = _RecordingTracer()
        container = _container(
            tracer,
            discovery=discovery,
            settings=settings,
            workspace_handler=_failing_handler(),
            provision_command_handler=_failing_handler(),
            lint_handler=_failing_handler(),
        )
        return _outcome(_invoke(container, *args)), tracer.envs

    failing = _Discovery([], error=RuntimeError("workspace unreadable"))
    off, off_envs = run(TracingSettings(), _Discovery(["alpha", "beta"]))
    on, on_envs = run(_TRACING_ON, failing)

    assert failing.runs >= 1
    assert off[0] != 0
    assert on == off
    assert on_envs == [] == off_envs


def test_a_failure_resolving_the_env_target_service_changes_nothing() -> None:
    def broken_service() -> Any:
        raise RuntimeError("cannot build the service")

    def run(*, broken: bool) -> tuple[int, str, str]:
        tracer = _RecordingTracer()
        extra: dict[str, Any] = {}
        container = _container(tracer, workspace_handler=_failing_handler(), **extra)
        if broken:
            container.env_target_service.override(providers.Callable(broken_service))
        return _outcome(_invoke(container, "ws", "status", "alpha"))

    assert run(broken=True) == run(broken=False)
    assert run(broken=True)[0] != 0


def test_the_parsed_values_are_unchanged_by_the_declaration() -> None:
    tracer = _RecordingTracer()
    handler = MagicMock()

    _invoke(_container(tracer, workspace_handler=handler), "ws", "connect", "alpha", "beta", "feature/x")

    params = handler.connect.call_args.args[0]
    assert params.patterns == ["alpha", "beta"]
    assert params.feature_branch == "feature/x"
