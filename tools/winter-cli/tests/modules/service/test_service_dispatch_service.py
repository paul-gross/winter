from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeSpecLoader, FakeSubprocessRunner
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

WS = Path("/ws")


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


def _make_registry_and_resolver(
    *,
    orchestrator: str | None,
    repos: list[StandaloneRepository],
    manifests: dict[Path, dict],
    files: dict[Path, str],
) -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(manifests))
    fs = FakeFilesystem(files=files)
    bindings: dict[str, list[str]] = {"service": [orchestrator]} if orchestrator else {}
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        bindings=bindings,
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    resolver = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory(repos),
        manifest_loader=loader,
        fs=fs,
    )
    return registry, resolver


def _tmux_repo() -> StandaloneRepository:
    return StandaloneRepository(name="winter-service-tmux", path=WS / "winter-service-tmux")


def _configured_registry_and_resolver() -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """A fully-wired registry + resolver whose orchestrator declares `orchestrate_services = 'workflow/service'`."""
    repo = _tmux_repo()
    entrypoint = repo.path / "workflow/service"
    return _make_registry_and_resolver(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={repo.path / EXT_MANIFEST: "", entrypoint: ""},
    )


def _fan_out_svc(runner: FakeSubprocessRunner) -> ServiceFanOutService:
    """Build a ServiceFanOutService."""
    return ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
    )


def _service(runner: FakeSubprocessRunner | None = None) -> ServiceDispatchService:
    _runner = runner or FakeSubprocessRunner()
    _registry, resolver = _configured_registry_and_resolver()
    describe_svc = ServiceDescribeService(
        subprocess_runner=_runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    return ServiceDispatchService(
        subprocess_runner=_runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(_runner),
        describe_service=describe_svc,
        workspace_root=WS,
    )


# ── happy path: dispatch, env var forwarding, exit-code passthrough ───────────


def test_dispatch_up_executes_entrypoint_with_action_and_env() -> None:
    runner = FakeSubprocessRunner()
    code = _service(runner).dispatch("up", ["alpha"])
    assert code == 0
    # The first call_calls entry is the up call.
    assert runner.call_calls[0] == ([str(WS / "winter-service-tmux/workflow/service"), "up", "alpha"], WS)


def test_dispatch_down_executes_entrypoint_with_action_and_env() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("down", ["beta"])
    assert runner.call_calls == [([str(WS / "winter-service-tmux/workflow/service"), "down", "beta"], WS)]


def test_dispatch_restart_with_patterns_passes_them_on_argv() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("restart", ["alpha/api", "*/backend"])
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "restart", "alpha/api", "*/backend"], WS)]
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_NAME" not in env
    assert "WINTER_SERVICE_PATTERNS" not in env


def test_dispatch_status_with_patterns_passes_them_on_argv() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("status", ["alpha/web", "alpha/api"])
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "status", "alpha/web", "alpha/api"], WS)]


def test_dispatch_status_with_no_positionals_omits_them() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("status", [])
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "status"], WS)]


def test_dispatch_preserves_inherited_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: dispatch must not wipe the parent environment."""
    monkeypatch.setenv("WINTER_TEST_SENTINEL", "canary-value")
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("restart", ["alpha/worker"])
    assert len(runner.call_envs) == 1
    env = runner.call_envs[0]
    assert env["WINTER_TEST_SENTINEL"] == "canary-value"
    assert env.items() >= os.environ.items()


def test_dispatch_passes_exit_code_through_unmodified() -> None:
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    runner = FakeSubprocessRunner(call_responses={f"{entrypoint} status": 3})
    assert _service(runner).dispatch("status", []) == 3


def test_dispatch_sets_workspace_context_env_vars() -> None:
    """Dispatch injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, and cwd."""
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("up", ["alpha"])
    # call_envs[0] is the up call env.
    assert len(runner.call_envs) >= 1
    env = runner.call_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"
    assert runner.call_calls[0][1] == WS


def test_dispatch_no_selection_env_vars_for_up() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("up", ["alpha"])
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_NAME" not in env
    assert "WINTER_SERVICE_PATTERNS" not in env


def test_dispatch_no_selection_env_vars_for_down() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("down", ["alpha"])
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_NAME" not in env
    assert "WINTER_SERVICE_PATTERNS" not in env


# ── leading-dash pattern forwarding ──────────────────────────────────────────


def test_dispatch_forwards_leading_dash_token_verbatim() -> None:
    """A leading-`-` pattern token is forwarded verbatim on argv without mangling.

    At the Click boundary a bare `-`-leading token is rejected as an unknown option
    (exit 2); the caller must use `--` to pass it through. Winter then forwards the
    token verbatim as a positional argv element — it never reinterprets it.
    """
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("restart", ["-weird"])
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "restart", "-weird"], WS)]


# ── misconfiguration errors (tested via the registry) ────────────────────────


def _service_for_error(
    *,
    orchestrator: str | None,
    repos: list[StandaloneRepository],
    manifests: dict[Path, dict],
    files: dict[Path, str],
) -> ServiceDispatchService:
    """Build a ServiceDispatchService configured for error-path testing."""
    runner = FakeSubprocessRunner()
    _registry, resolver = _make_registry_and_resolver(
        orchestrator=orchestrator,
        repos=repos,
        manifests=manifests,
        files=files,
    )
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    return ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(runner),
        describe_service=describe_svc,
        workspace_root=WS,
    )


def test_no_orchestrator_registered_raises() -> None:
    svc = _service_for_error(orchestrator=None, repos=[], manifests={}, files={})
    with pytest.raises(RepoError, match="no extension provides"):
        svc.dispatch("up", ["alpha"])


def test_unknown_extension_name_raises() -> None:
    svc = _service_for_error(
        orchestrator="winter-service-docker",
        repos=[_tmux_repo()],
        manifests={},
        files={},
    )
    with pytest.raises(RepoError, match="no installed extension named"):
        svc.dispatch("up", ["alpha"])


def test_extension_missing_service_key_raises() -> None:
    repo = _tmux_repo()
    svc = _service_for_error(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {}},
        files={repo.path / EXT_MANIFEST: ""},
    )
    with pytest.raises(RepoError, match=r"declares no provides\.service"):
        svc.dispatch("up", ["alpha"])


def test_missing_entrypoint_file_raises() -> None:
    repo = _tmux_repo()
    svc = _service_for_error(
        orchestrator="winter-service-tmux",
        repos=[repo],
        manifests={repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}},
        files={repo.path / EXT_MANIFEST: ""},  # manifest present, entrypoint absent
    )
    with pytest.raises(RepoError, match="entrypoint not found"):
        svc.dispatch("up", ["alpha"])
