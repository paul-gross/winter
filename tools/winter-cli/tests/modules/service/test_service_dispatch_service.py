from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeEnvIndexRegistry,
    FakeFilesystem,
    FakeServiceReporter,
    FakeSpecLoader,
    FakeSubprocessRunner,
)
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_reporter import IServiceReporter
from winter_cli.modules.service.service_status_matrix_service import ServiceStatusMatrixService
from winter_cli.modules.service.status_parser import StatusDocumentParser
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

WS = Path("/ws")
SERVICE_PREFIX = "winter"


class _FakeEnvProvisionerService:
    """No-op env provisioner: computes an empty env map for every scope."""

    def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
        return {}


def _matrix_svc(runner: FakeSubprocessRunner, assignments: dict[str, int] | None = None) -> ServiceStatusMatrixService:
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    registry = FakeEnvIndexRegistry(assignments or {"alpha": 1, "beta": 2})
    return ServiceStatusMatrixService(
        subprocess_runner=runner,
        describe_service=describe_svc,
        env_provisioner=_FakeEnvProvisionerService(),
        status_parser=StatusDocumentParser(),
        env_index_registry=registry,
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos

    def get_extension_repos(self) -> list[StandaloneRepository]:
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


def _provider(name: str, entrypoint: Path, ext_dir: Path) -> ResolvedCapability:
    return ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name=name,
        entrypoint=entrypoint,
        ext_dir=ext_dir,
        prefix=name,
        config_dir=WS / ".winter" / "config" / name,
    )


class _StubResolver:
    """Minimal ServiceOrchestratorResolver stub: resolve_all() returns a fixed list."""

    def __init__(self, providers: list[ResolvedCapability]) -> None:
        self._providers = providers

    def resolve_all(self) -> list[ResolvedCapability]:
        return list(self._providers)

    def resolve(self) -> ResolvedCapability:
        return self._providers[0]


def _dispatch_svc(
    runner: FakeSubprocessRunner,
    providers: list[ResolvedCapability],
    *,
    assignments: dict[str, int] | None = None,
    reporter: IServiceReporter | None = None,
) -> ServiceDispatchService:
    """Build a ServiceDispatchService wired directly with *providers* (bypassing the registry)."""
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    return ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=_StubResolver(providers),  # type: ignore[arg-type]
        fan_out_service=_fan_out_svc(runner),
        describe_service=describe_svc,
        matrix_service=_matrix_svc(runner, assignments),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
        reporter=reporter,
    )


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
        service_prefix=SERVICE_PREFIX,
    )


def _service(runner: FakeSubprocessRunner | None = None) -> ServiceDispatchService:
    _runner = runner or FakeSubprocessRunner()
    _registry, resolver = _configured_registry_and_resolver()
    describe_svc = ServiceDescribeService(
        subprocess_runner=_runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    return ServiceDispatchService(
        subprocess_runner=_runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(_runner),
        describe_service=describe_svc,
        matrix_service=_matrix_svc(_runner),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
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


def test_dispatch_restart_sets_workspace_context_env_vars() -> None:
    """restart dispatch (single-provider short-circuit) still receives the base extension
    vars, including WINTER_SERVICE_PREFIX — it is workspace-invariant, not a scope var
    withheld from restart/logs."""
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("restart", ["alpha/api"])
    env = runner.call_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"
    assert env["WINTER_SERVICE_PREFIX"] == SERVICE_PREFIX


# ── restart pattern validation (winter#149) ──────────────────────────────────


def _single_provider() -> ResolvedCapability:
    return _provider(
        "winter-service-tmux",
        WS / "winter-service-tmux/workflow/service",
        WS / "winter-service-tmux",
    )


def test_dispatch_restart_bare_unmatched_token_errors_single_provider() -> None:
    """A bare token is read as the env query ``repo-name/*``, matches no configured
    env, and is rejected rather than silently dropped while the sibling `alpha`
    token restarts the whole env (single provider).
    """
    runner = FakeSubprocessRunner()
    reporter = FakeServiceReporter()
    svc = _dispatch_svc(runner, [_single_provider()], assignments={"alpha": 1, "beta": 2}, reporter=reporter)
    code = svc.dispatch("restart", ["alpha", "repo-name"])

    assert code != 0
    # Nothing was dispatched — the valid `alpha` token must not restart the whole env.
    assert runner.call_calls == []
    assert len(reporter.invalid_restart_pattern_calls) == 1
    detail = reporter.invalid_restart_pattern_calls[0]
    # Diagnosed as an unknown environment, suggesting the token as a service
    # under a real configured env — not a "no such service" claim.
    assert detail == "'repo-name' is not a valid environment — did you mean 'alpha/repo-name'?"


def test_dispatch_restart_valid_qualified_pattern_restarts_exactly_that_service_single_provider() -> None:
    """A valid qualified pattern (`alpha/repo-name`) restarts exactly that service."""
    runner = FakeSubprocessRunner()
    svc = _dispatch_svc(runner, [_single_provider()], assignments={"alpha": 1})
    code = svc.dispatch("restart", ["alpha/repo-name"])

    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "restart", "alpha/repo-name"], WS)]


def test_dispatch_restart_multiple_valid_qualified_patterns_all_restart_single_provider() -> None:
    """Multiple valid qualified patterns in one invocation all restart."""
    runner = FakeSubprocessRunner()
    svc = _dispatch_svc(runner, [_single_provider()], assignments={"alpha": 1})
    code = svc.dispatch("restart", ["alpha/api", "alpha/web"])

    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "restart", "alpha/api", "alpha/web"], WS)]


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
    """Dispatch injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, WINTER_SERVICE_PREFIX, and cwd."""
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("up", ["alpha"])
    # call_envs[0] is the up call env.
    assert len(runner.call_envs) >= 1
    env = runner.call_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert env["WINTER_EXT_DIR"] == str(WS / "winter-service-tmux")
    assert env["WINTER_EXT_PREFIX"] == "winter-service-tmux"
    assert env["WINTER_SERVICE_PREFIX"] == SERVICE_PREFIX
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


# ── WINTER_SERVICE_TIMEOUT injection (up only) ───────────────────────────────


def test_dispatch_up_injects_default_timeout_env_var() -> None:
    """`up` with no explicit timeout_s injects the DEFAULT_WAIT_TIMEOUT_S default (120.0)."""
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("up", ["alpha"])
    env = runner.call_envs[0]
    assert env["WINTER_SERVICE_TIMEOUT"] == "120.0"


def test_dispatch_up_injects_custom_timeout_env_var() -> None:
    """`up` forwards a caller-supplied timeout_s verbatim as a plain float string."""
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("up", ["alpha"], 45.0)
    env = runner.call_envs[0]
    assert env["WINTER_SERVICE_TIMEOUT"] == "45.0"


def test_dispatch_down_does_not_inject_timeout_env_var() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("down", ["alpha"])
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_TIMEOUT" not in env


def test_dispatch_status_does_not_inject_timeout_env_var() -> None:
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("status", ["alpha"])
    env = runner.call_envs[0]
    assert "WINTER_SERVICE_TIMEOUT" not in env


# ── leading-dash pattern forwarding ──────────────────────────────────────────


def test_dispatch_forwards_leading_dash_token_verbatim() -> None:
    """A leading-`-` pattern token is forwarded verbatim on argv without mangling.

    At the Click boundary a bare `-`-leading token is rejected as an unknown option
    (exit 2); the caller must use `--` to pass it through. Winter then forwards the
    token verbatim as a positional argv element — it never reinterprets it. The
    ``workspace/`` scope prefix keeps the pattern valid under restart's env-segment
    validation (winter#149) so this test still exercises argv passthrough rather
    than the (separately tested) unmatched-pattern error path.
    """
    runner = FakeSubprocessRunner()
    _service(runner).dispatch("restart", ["workspace/-weird"])
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "restart", "workspace/-weird"], WS)]


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
        service_prefix=SERVICE_PREFIX,
    )
    return ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(runner),
        describe_service=describe_svc,
        matrix_service=_matrix_svc(runner),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
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


# ── up/down: multi-env, glob, no-match (winter#139) ───────────────────────────


def test_dispatch_up_multi_env_patterns_starts_each_matched_env() -> None:
    """up alpha beta -> both matched envs start; gamma (untargeted) is untouched."""
    runner = FakeSubprocessRunner()
    svc = _service(runner)  # single provider, registry has alpha=1, beta=2
    code = svc.dispatch("up", ["alpha", "beta"])
    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (entrypoint, "up", "alpha"),
        (entrypoint, "up", "beta"),
    ]


def test_dispatch_up_glob_pattern_starts_only_matching_envs() -> None:
    """up 'al*' with configured envs alpha/beta/gamma -> only alpha starts."""
    runner = FakeSubprocessRunner()
    _registry, resolver = _configured_registry_and_resolver()
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    svc = ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(runner),
        describe_service=describe_svc,
        matrix_service=_matrix_svc(runner, {"alpha": 1, "beta": 2, "gamma": 3}),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    code = svc.dispatch("up", ["al*"])
    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    assert runner.call_calls == [([entrypoint, "up", "alpha"], WS)]


def test_dispatch_down_bare_wildcard_stops_every_configured_env() -> None:
    """down '*' stops every configured feature env but never the workspace scope."""
    runner = FakeSubprocessRunner()
    svc = ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=_configured_registry_and_resolver()[1],
        fan_out_service=_fan_out_svc(runner),
        describe_service=ServiceDescribeService(
            subprocess_runner=runner,
            describe_parser=DescribeResultParser(),
            workspace_root=WS,
            service_prefix=SERVICE_PREFIX,
        ),
        matrix_service=_matrix_svc(runner, {"alpha": 1, "beta": 2}),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
    )
    code = svc.dispatch("down", ["*"])
    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (entrypoint, "down", "alpha"),
        (entrypoint, "down", "beta"),
    ]


# ── up/down: multi-filter same-scope must not degrade to whole-scope (pre-push MUST-FIX) ──


def test_dispatch_down_multi_filter_same_scope_dispatches_each_service_not_whole_scope() -> None:
    """down alpha/db alpha/api must dispatch exactly those two services, never `down alpha`.

    Regression for the pre-push MUST-FIX: the status matrix's `_cell_argv_pattern`
    collapses 2+ same-scope service filters to `alpha/*` (relying on a post-merge
    backstop `status` has and up/down does not); `up_down_positional` then collapses
    `alpha/*` back to the bare scope `alpha`, so `down alpha/db alpha/api` used to
    dispatch `down alpha` — stopping the ENTIRE alpha env instead of just db/api.
    """
    runner = FakeSubprocessRunner()
    svc = _service(runner)  # single provider, registry has alpha=1, beta=2
    code = svc.dispatch("down", ["alpha/db", "alpha/api"])
    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (entrypoint, "down", "alpha/db"),
        (entrypoint, "down", "alpha/api"),
    ]
    # The bare, whole-scope form must never be dispatched.
    assert (entrypoint, "down", "alpha") not in call_cmds


def test_dispatch_up_multi_pattern_cross_env_dispatches_each_service_per_matched_env() -> None:
    """up '*/api' '*/web' -> each matched env (alpha, beta) gets its own api and web cells.

    Each matched scope carries 2 service-segment filters (api, web); neither may
    degrade to the whole scope (`up alpha` / `up beta`).
    """
    runner = FakeSubprocessRunner()
    svc = _service(runner)  # single provider, registry has alpha=1, beta=2
    code = svc.dispatch("up", ["*/api", "*/web"])
    assert code == 0
    entrypoint = str(WS / "winter-service-tmux/workflow/service")
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (entrypoint, "up", "alpha/api"),
        (entrypoint, "up", "alpha/web"),
        (entrypoint, "up", "beta/api"),
        (entrypoint, "up", "beta/web"),
    ]
    assert (entrypoint, "up", "alpha") not in call_cmds
    assert (entrypoint, "up", "beta") not in call_cmds


def test_dispatch_up_no_matching_scope_reports_diagnostic_and_returns_1() -> None:
    """A pattern matching no configured env reports a diagnostic and returns 1."""
    runner = FakeSubprocessRunner()
    reporter = FakeServiceReporter()
    _registry, resolver = _configured_registry_and_resolver()
    svc = ServiceDispatchService(
        subprocess_runner=runner,
        orchestrator_resolver=resolver,
        fan_out_service=_fan_out_svc(runner),
        describe_service=ServiceDescribeService(
            subprocess_runner=runner,
            describe_parser=DescribeResultParser(),
            workspace_root=WS,
            service_prefix=SERVICE_PREFIX,
        ),
        matrix_service=_matrix_svc(runner, {"alpha": 1, "beta": 2}),
        workspace_root=WS,
        service_prefix=SERVICE_PREFIX,
        reporter=reporter,  # type: ignore[arg-type]
    )
    code = svc.dispatch("up", ["zzz"])
    assert code == 1
    assert runner.call_calls == []
    assert reporter.no_service_matched_calls == ["'zzz'"]


# ── up/down: multi-provider enumeration (winter#139) ──────────────────────────

_EP_A = str(WS / "provider-a/workflow/service")
_EP_B = str(WS / "provider-b/workflow/service")


def _two_providers() -> list[ResolvedCapability]:
    return [
        _provider("provider-a", Path(_EP_A), WS / "provider-a"),
        _provider("provider-b", Path(_EP_B), WS / "provider-b"),
    ]


def _describe_response(*services: str) -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout=json.dumps({"services": list(services)}), stderr="")


def test_dispatch_up_multi_provider_dispatches_every_owning_provider() -> None:
    """Two providers each owning env-scoped services both get an up cell for the matched env."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{_EP_A} describe": _describe_response("*/db"),
            f"{_EP_B} describe": _describe_response("*/api"),
        }
    )
    svc = _dispatch_svc(runner, _two_providers(), assignments={"alpha": 1})
    code = svc.dispatch("up", ["alpha"])
    assert code == 0
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (_EP_A, "up", "alpha"),
        (_EP_B, "up", "alpha"),
    ]


def test_dispatch_up_aborts_on_first_cell_failure_multi_provider() -> None:
    """up forward-fanout aborts on the first cell's non-zero exit; later cells never dispatch."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{_EP_A} describe": _describe_response("*/db"),
            f"{_EP_B} describe": _describe_response("*/api"),
        },
        call_responses={f"{_EP_A} up alpha": 9},
    )
    svc = _dispatch_svc(runner, _two_providers(), assignments={"alpha": 1})
    code = svc.dispatch("up", ["alpha"])
    assert code == 9
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "up", "alpha") in call_cmds
    assert (_EP_B, "up", "alpha") not in call_cmds


def test_dispatch_down_best_effort_continues_across_cells_multi_provider() -> None:
    """down is best-effort across cells: a failing cell does not stop the others."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{_EP_A} describe": _describe_response("*/db"),
            f"{_EP_B} describe": _describe_response("*/api"),
        },
        call_responses={f"{_EP_A} down alpha": 4},
    )
    svc = _dispatch_svc(runner, _two_providers(), assignments={"alpha": 1})
    code = svc.dispatch("down", ["alpha"])
    assert code == 4
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_B, "down", "alpha") in call_cmds
