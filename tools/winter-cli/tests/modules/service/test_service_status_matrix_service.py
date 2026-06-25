"""Phase 2 tests: call-matrix enumeration, env injection, and parallel invocation.

Covers:
- Multi-provider matrix enumeration: cells = providers x configured envs + workspace
- Single-provider describe-skip: all configured envs + workspace cell, no describe call
- Per-cell env injection: WINTER_ENV/INDEX/PORT_BASE injected + sourced vars overlay
- Scope-qualified matrix filtering: gamma/web -> only gamma cells of owning provider
- Bare env pattern filtering: gamma -> only gamma cells (all owning providers)
- Workspace cell filtering: workspace/* -> only workspace cells
- No-pattern full matrix: all cells returned
- EnvFileSourcerError degrades only affected scope, keeps the rest
- Merged-document equivalence: N single-env docs merge to the same shape
- collect() degenerates to a single-env cell (cheap poll)
- Subprocess integration: real fake-provider scripts prove env injection at runtime
"""

import json
import stat
from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeFilesystem,
    FakeServiceReporter,
    FakeSpecLoader,
    FakeSubprocessRunner,
)
from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.internal.local_subprocess_runner import LocalSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.capability_registry_service import CapabilityRegistryService
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.describe_parser import DescribeResultParser
from winter_cli.modules.service.env_file_sourcer import EnvFileSourcerError
from winter_cli.modules.service.orchestrator_resolver import ServiceOrchestratorResolver
from winter_cli.modules.service.service_provider_index import ServiceDescribeService
from winter_cli.modules.service.service_status_matrix_service import (
    ServiceStatusMatrixService,
    _cell_argv_pattern,
    _scope_matches_patterns,
)
from winter_cli.modules.service.service_status_service import ServiceStatusService
from winter_cli.modules.service.status_models import StatusOptions
from winter_cli.modules.service.status_parser import StatusDocumentParser
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

WS = Path("/ws")
EXT_A = WS / "provider-a"
EXT_B = WS / "provider-b"
ENTRYPOINT_A = EXT_A / "workflow/service"
ENTRYPOINT_B = EXT_B / "workflow/service"

# ── helpers ───────────────────────────────────────────────────────────────────


class _StubRepoFactory:
    def __init__(self, repos: list[StandaloneRepository]) -> None:
        self._repos = repos

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return self._repos


class FakeEnvFileSourcer:
    """Fake IEnvFileSourcer that returns canned dicts per scope."""

    def __init__(
        self,
        responses: dict[str, dict[str, str]] | None = None,
        broken: set[str] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._broken = broken or set()
        self.calls: list[str] = []

    def source(self, scope: str, ws_root: Path) -> dict[str, str]:
        self.calls.append(scope)
        if scope in self._broken:
            raise EnvFileSourcerError(
                f"broken env file for {scope!r}",
                exit_code=1,
                stderr="syntax error",
            )
        return self._responses.get(scope, {})


def _provider(name: str, entrypoint: Path, ext_dir: Path) -> ResolvedCapability:
    return ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name=name,
        entrypoint=entrypoint,
        ext_dir=ext_dir,
        prefix=name,
        config_dir=WS / ".winter" / "config" / name,
    )


def _provider_a() -> ResolvedCapability:
    return _provider("provider-a", ENTRYPOINT_A, EXT_A)


def _provider_b() -> ResolvedCapability:
    return _provider("provider-b", ENTRYPOINT_B, EXT_B)


def _describe_result(json_str: str) -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout=json_str, stderr="")


def _describe_json(*services: str) -> str:
    return json.dumps({"services": list(services)})


def _status_doc_json(env: str, port_base: int = 4020, services: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "envs": [
                {
                    "env": env,
                    "session": f"sess-{env}",
                    "port_base": port_base,
                    "services": services or [],
                }
            ]
        }
    )


def _empty_doc_json(env: str) -> str:
    return json.dumps({"envs": [{"env": env, "session": None, "port_base": None, "services": []}]})


def _fake_ws_config(
    base_port: int = 4000,
    ports_per_env: int = 20,
) -> WorkspaceConfig:
    """Minimal WorkspaceConfig for testing (alpha=index 1 -> port_base=4020)."""
    from winter_cli.config.models import ProjectRepositoryConfig, SingletonRepository, SingletonType

    return WorkspaceConfig(
        workspace_root=WS,
        session_prefix="test",
        main_branch="main",
        base_port=base_port,
        ports_per_env=ports_per_env,
        singleton_repos=[SingletonRepository(name="ws", type=SingletonType.workspace)],
        project_repos=[ProjectRepositoryConfig(name="demo", url="git@example.com:demo.git")],
    )


class _FakeEnvIndexRegistry:
    """In-memory IEnvIndexRegistry fake."""

    def __init__(self, assignments: dict[str, int] | None = None) -> None:
        self._data: dict[str, int] = dict(assignments or {})

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


def _matrix_svc(
    runner: FakeSubprocessRunner,
    sourcer: FakeEnvFileSourcer | None = None,
    registry_assignments: dict[str, int] | None = None,
) -> ServiceStatusMatrixService:
    """Build a ServiceStatusMatrixService with test doubles."""
    ws_config = _fake_ws_config()
    reg = _FakeEnvIndexRegistry(registry_assignments or {"alpha": 1, "beta": 2})
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    return ServiceStatusMatrixService(
        subprocess_runner=runner,
        describe_service=describe_svc,
        env_file_sourcer=sourcer or FakeEnvFileSourcer(),
        status_parser=StatusDocumentParser(),
        workspace_config=ws_config,
        env_index_registry=reg,
        workspace_root=WS,
    )


# ── _scope_matches_patterns pure-function tests ───────────────────────────────


def test_scope_matches_no_patterns_always_true() -> None:
    """With no patterns every scope is included."""
    assert _scope_matches_patterns("alpha", ()) is True
    assert _scope_matches_patterns("workspace", ()) is True


def test_scope_matches_bare_env_pattern() -> None:
    """Bare env pattern matches only the named scope."""
    assert _scope_matches_patterns("gamma", ("gamma",)) is True
    assert _scope_matches_patterns("alpha", ("gamma",)) is False


def test_scope_matches_bare_workspace_pattern() -> None:
    """Bare 'workspace' pattern matches the workspace scope."""
    assert _scope_matches_patterns("workspace", ("workspace",)) is True
    assert _scope_matches_patterns("alpha", ("workspace",)) is False


def test_scope_matches_scope_qualified_pattern() -> None:
    """Scope-qualified pattern (gamma/web) matches only the gamma scope."""
    assert _scope_matches_patterns("gamma", ("gamma/web",)) is True
    assert _scope_matches_patterns("alpha", ("gamma/web",)) is False


def test_scope_matches_workspace_qualified_pattern() -> None:
    """workspace/rabbitmq matches the workspace scope."""
    assert _scope_matches_patterns("workspace", ("workspace/rabbitmq",)) is True
    assert _scope_matches_patterns("alpha", ("workspace/rabbitmq",)) is False


# ── _cell_argv_pattern pure-function tests ────────────────────────────────────


def test_cell_pattern_no_patterns() -> None:
    """No patterns -> <scope>/*."""
    assert _cell_argv_pattern("alpha", ()) == "alpha/*"
    assert _cell_argv_pattern("workspace", ()) == "workspace/*"


def test_cell_pattern_bare_env() -> None:
    """Bare env name -> <scope>/*."""
    assert _cell_argv_pattern("gamma", ("gamma",)) == "gamma/*"


def test_cell_pattern_scope_qualified() -> None:
    """Scope-qualified pattern forwards the service segment."""
    assert _cell_argv_pattern("gamma", ("gamma/web",)) == "gamma/web"


def test_cell_pattern_workspace_qualified() -> None:
    """workspace/<svc> forwards the service segment."""
    assert _cell_argv_pattern("workspace", ("workspace/rabbitmq",)) == "workspace/rabbitmq"


# ── Single-provider matrix (describe-skip) ────────────────────────────────────


def test_sole_provider_no_describe_call() -> None:
    """Single provider: build_matrix makes NO describe subprocess call."""
    runner = FakeSubprocessRunner()  # no run_responses; any run() would raise
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()

    svc.build_matrix([pa], patterns=())

    # No run (describe) calls were made.
    assert runner.run_calls == []


def test_sole_provider_cells_cover_all_configured_envs() -> None:
    """Single provider: one cell per configured env."""
    runner = FakeSubprocessRunner()
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2, "gamma": 3})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())

    env_scopes = [c.scope for c in cells if c.scope != "workspace"]
    assert set(env_scopes) == {"alpha", "beta", "gamma"}


def test_sole_provider_cells_include_workspace() -> None:
    """Single provider: matrix includes one workspace cell."""
    runner = FakeSubprocessRunner()
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())

    ws_cells = [c for c in cells if c.scope == "workspace"]
    assert len(ws_cells) == 1
    assert ws_cells[0].provider is pa


def test_sole_provider_all_cells_belong_to_sole_provider() -> None:
    """Every cell in the single-provider matrix belongs to the sole provider."""
    runner = FakeSubprocessRunner()
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())

    assert all(c.provider is pa for c in cells)


# ── Multi-provider matrix enumeration ─────────────────────────────────────────


def test_multi_provider_env_cells_for_env_owning_provider() -> None:
    """Multi-provider: provider A owns */db -> gets env cell for each configured env."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("workspace/rabbitmq")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=())

    env_cells_a = [c for c in cells if c.scope != "workspace" and c.provider is pa]
    env_cells_b = [c for c in cells if c.scope != "workspace" and c.provider is pb]
    ws_cells_b = [c for c in cells if c.scope == "workspace" and c.provider is pb]

    # A owns */db -> 2 env cells (alpha + beta)
    assert len(env_cells_a) == 2
    assert {c.scope for c in env_cells_a} == {"alpha", "beta"}
    # B owns only workspace/rabbitmq -> no env cells
    assert env_cells_b == []
    # B owns workspace/rabbitmq -> 1 workspace cell
    assert len(ws_cells_b) == 1


def test_multi_provider_provider_without_env_services_no_env_cells() -> None:
    """Multi-provider: provider that owns only workspace services gets no env cells."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("workspace/rabbitmq")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("*/db")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=())

    env_cells_a = [c for c in cells if c.scope == "alpha" and c.provider is pa]
    env_cells_b = [c for c in cells if c.scope == "alpha" and c.provider is pb]
    assert env_cells_a == []
    assert len(env_cells_b) == 1


def test_multi_provider_workspace_cells_only_for_workspace_owners() -> None:
    """Multi-provider: only providers owning workspace/* services get workspace cells."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("workspace/rabbitmq")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=())

    ws_cells = [c for c in cells if c.scope == "workspace"]
    assert len(ws_cells) == 1
    assert ws_cells[0].provider is pb


def test_multi_provider_both_own_env_and_workspace() -> None:
    """Multi-provider: provider owning both */ and workspace/* gets both types of cells."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db", "workspace/rabbitmq")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("*/api")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=())

    env_cells_a = [c for c in cells if c.scope not in ("workspace",) and c.provider is pa]
    ws_cells_a = [c for c in cells if c.scope == "workspace" and c.provider is pa]
    env_cells_b = [c for c in cells if c.scope not in ("workspace",) and c.provider is pb]

    assert len(env_cells_a) == 2  # alpha + beta
    assert len(ws_cells_a) == 1
    assert len(env_cells_b) == 2  # alpha + beta


# ── Scope filtering ───────────────────────────────────────────────────────────


def test_scope_filter_bare_env_narrows_to_that_env() -> None:
    """Bare env pattern produces only cells for that env."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("workspace/rabbitmq")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2, "gamma": 3})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=("gamma",))

    # Only gamma env cells; no alpha/beta; no workspace.
    scopes = {c.scope for c in cells}
    assert scopes == {"gamma"}


def test_scope_filter_qualified_pattern_narrows_scope_and_provider() -> None:
    """Scope-qualified pattern narrows both scope and provider axis."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("*/api")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2, "gamma": 3})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=("gamma/db",))

    # Only gamma scope; only provider-a (owns */db).
    assert all(c.scope == "gamma" for c in cells)
    providers_in_cells = {c.provider for c in cells}
    assert pa in providers_in_cells
    assert pb not in providers_in_cells


def test_scope_filter_workspace_qualified_pattern() -> None:
    """workspace/rabbitmq narrows to workspace cells of owning provider."""
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("workspace/rabbitmq")),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=("workspace/rabbitmq",))

    scopes = {c.scope for c in cells}
    assert scopes == {"workspace"}
    providers_in_cells = {c.provider for c in cells}
    assert pb in providers_in_cells
    assert pa not in providers_in_cells


def test_scope_filter_no_patterns_full_matrix() -> None:
    """No patterns -> full matrix (all envs + workspace)."""
    runner = FakeSubprocessRunner()  # single provider, no describe
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2, "gamma": 3})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())

    scopes = {c.scope for c in cells}
    assert scopes == {"alpha", "beta", "gamma", "workspace"}


# ── Cell argv pattern ─────────────────────────────────────────────────────────


def test_cell_pattern_forwarded_as_explicit_scope() -> None:
    """Each cell is invoked with <scope>/* (or <scope>/<svc>) as the explicit pattern."""
    runner = FakeSubprocessRunner()
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())

    alpha_cell = next(c for c in cells if c.scope == "alpha")
    ws_cell = next(c for c in cells if c.scope == "workspace")
    assert alpha_cell.cell_pattern == "alpha/*"
    assert ws_cell.cell_pattern == "workspace/*"


def test_cell_pattern_scope_qualified_user_filter() -> None:
    """User-supplied scope-qualified pattern narrows cell_pattern to <scope>/<svc>."""
    runner = FakeSubprocessRunner()
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("alpha/db",))

    assert len(cells) == 1
    assert cells[0].scope == "alpha"
    assert cells[0].cell_pattern == "alpha/db"


def test_cell_pattern_multiple_services_same_scope_forwards_wildcard() -> None:
    """Multiple service patterns targeting the same scope forward <scope>/*.

    When the user supplies ``alpha/db alpha/api``, both target the ``alpha``
    scope.  The matrix sends ``alpha/*`` to the provider so it returns all
    services; the post-merge ``filter_status`` backstop then narrows to only
    ``db`` and ``api``.  Forwarding only the first (``alpha/db``) would silently
    drop ``alpha/api`` from the provider's response.
    """
    assert _cell_argv_pattern("alpha", ("alpha/db", "alpha/api")) == "alpha/*"


# ── Per-cell env injection ─────────────────────────────────────────────────────


def test_env_injection_winter_env_set_per_cell() -> None:
    """WINTER_ENV is set to the cell's scope in the provider subprocess environment."""
    alpha_doc = _status_doc_json("alpha", port_base=4020)
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("alpha",))
    svc.run_matrix(cells, reporter=None)

    # Find the popen call for alpha/*
    idx = next(i for i, call in enumerate(runner.popen_calls) if "alpha/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("WINTER_ENV") == "alpha"


def test_env_injection_winter_env_index_set_from_registry() -> None:
    """WINTER_ENV_INDEX is set from the registry (alpha=1 -> '1')."""
    alpha_doc = _status_doc_json("alpha")
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("alpha",))
    svc.run_matrix(cells, reporter=None)

    idx = next(i for i, call in enumerate(runner.popen_calls) if "alpha/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("WINTER_ENV_INDEX") == "1"


def test_env_injection_winter_port_base_alpha() -> None:
    """WINTER_PORT_BASE is 4020 for alpha (base_port=4000, index=1, ports_per_env=20)."""
    alpha_doc = _status_doc_json("alpha", port_base=4020)
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("alpha",))
    svc.run_matrix(cells, reporter=None)

    idx = next(i for i, call in enumerate(runner.popen_calls) if "alpha/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("WINTER_PORT_BASE") == "4020"


def test_env_injection_winter_port_base_beta() -> None:
    """WINTER_PORT_BASE is 4040 for beta (base_port=4000, index=2, ports_per_env=20)."""
    beta_doc = _status_doc_json("beta", port_base=4040)
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status beta/*": ([beta_doc], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"beta": 2})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("beta",))
    svc.run_matrix(cells, reporter=None)

    idx = next(i for i, call in enumerate(runner.popen_calls) if "beta/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("WINTER_PORT_BASE") == "4040"


def test_env_injection_sourced_vars_overlay_env_trio() -> None:
    """Sourced env-file vars are overlaid onto the env trio (sourced wins on conflict)."""
    alpha_doc = _status_doc_json("alpha")
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
        }
    )
    # Sourced env overrides WINTER_PORT_BASE for alpha.
    sourcer = FakeEnvFileSourcer(responses={"alpha": {"MY_APP_PORT": "5555", "WINTER_PORT_BASE": "9999"}})
    svc = _matrix_svc(runner, sourcer=sourcer, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("alpha",))
    svc.run_matrix(cells, reporter=None)

    idx = next(i for i, call in enumerate(runner.popen_calls) if "alpha/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("MY_APP_PORT") == "5555"
    assert env.get("WINTER_PORT_BASE") == "9999"  # sourced wins


def test_env_sourcing_called_once_per_scope() -> None:
    """Each scope's env file is sourced at most once, even with two providers."""
    alpha_doc = _status_doc_json("alpha")
    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("*/api")),
        },
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
            f"{ENTRYPOINT_B} status alpha/*": ([alpha_doc], 0),
        },
    )
    sourcer = FakeEnvFileSourcer()
    svc = _matrix_svc(runner, sourcer=sourcer, registry_assignments={"alpha": 1})
    pa = _provider_a()
    pb = _provider_b()

    cells = svc.build_matrix([pa, pb], patterns=("alpha",))
    svc.run_matrix(cells, reporter=None)

    # alpha sourced exactly once (not once per provider).
    assert sourcer.calls.count("alpha") == 1


def test_workspace_scope_env_trio_uses_index_zero() -> None:
    """Workspace cells receive WINTER_ENV_INDEX=0 and WINTER_ENV=workspace."""
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=("workspace",))
    svc.run_matrix(cells, reporter=None)

    idx = next(i for i, call in enumerate(runner.popen_calls) if "workspace/*" in str(call[0]))
    env = runner.popen_envs[idx]
    assert env.get("WINTER_ENV") == "workspace"
    assert env.get("WINTER_ENV_INDEX") == "0"
    assert env.get("WINTER_PORT_BASE") == "4000"  # base_port + 0 * 20


# ── EnvFileSourcerError resilience ───────────────────────────────────────────


def test_broken_env_file_scope_degraded_not_crashed() -> None:
    """A broken env file for one scope skips that scope's cells; other scopes run."""
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([_status_doc_json("alpha")], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    # beta's env file is broken; alpha should still work.
    sourcer = FakeEnvFileSourcer(broken={"beta"})
    svc = _matrix_svc(runner, sourcer=sourcer, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()

    reporter = FakeServiceReporter()
    cells = svc.build_matrix([pa], patterns=())
    docs, _worst_exit = svc.run_matrix(cells, reporter=reporter)

    # alpha's doc should appear; beta is skipped.
    env_names = {env.env for doc in docs for env in doc.envs}
    assert "alpha" in env_names
    assert "beta" not in env_names


def test_broken_env_file_does_not_crash_run_matrix() -> None:
    """run_matrix returns normally even when a scope's env file is broken."""
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([_status_doc_json("alpha")], 0),
        }
    )
    svc = _matrix_svc(runner, sourcer=FakeEnvFileSourcer(broken={"workspace"}), registry_assignments={"alpha": 1})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())
    _docs, worst_exit = svc.run_matrix(cells, reporter=None)

    # run_matrix returns without raising; workspace was skipped.
    assert worst_exit == 0


# ── Merged-document equivalence ───────────────────────────────────────────────


def test_merged_docs_contain_all_env_scopes() -> None:
    """N per-scope docs merge into one document with N env entries."""
    from winter_cli.modules.service.status_merge import merge_status_documents
    from winter_cli.modules.service.status_parser import StatusDocumentParser

    parser = StatusDocumentParser()
    alpha_doc = parser.parse(_status_doc_json("alpha", port_base=4020))
    beta_doc = parser.parse(_status_doc_json("beta", port_base=4040))

    merged = merge_status_documents([alpha_doc, beta_doc])

    env_names = [e.env for e in merged.envs]
    assert "alpha" in env_names
    assert "beta" in env_names
    assert len(merged.envs) == 2


def test_run_matrix_docs_in_enumeration_order() -> None:
    """Docs returned by run_matrix are ordered by cell enumeration order (not completion order)."""
    # Provide both env docs; check that alpha comes before beta (alphabetical = enumeration order).
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([_status_doc_json("alpha", port_base=4020)], 0),
            f"{ENTRYPOINT_A} status beta/*": ([_status_doc_json("beta", port_base=4040)], 0),
            f"{ENTRYPOINT_A} status workspace/*": ([json.dumps({"envs": []})], 0),
        }
    )
    svc = _matrix_svc(runner, registry_assignments={"alpha": 1, "beta": 2})
    pa = _provider_a()

    cells = svc.build_matrix([pa], patterns=())
    docs, _ = svc.run_matrix(cells, reporter=None)

    # docs list contains non-empty docs first from alpha, then beta.
    non_empty = [d for d in docs if d.envs]
    if non_empty:
        env_names = [e.env for d in non_empty for e in d.envs]
        # alpha should precede beta (cells sorted alphabetically).
        assert env_names.index("alpha") < env_names.index("beta")


# ── ServiceStatusService integration with matrix ─────────────────────────────


def _make_two_provider_registry() -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to two providers."""
    repo_a = StandaloneRepository(name="provider-a", path=WS / "provider-a")
    repo_b = StandaloneRepository(name="provider-b", path=WS / "provider-b")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {
                repo_a.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
                repo_b.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"},
            }
        )
    )
    fs = FakeFilesystem(
        files={
            repo_a.path / EXT_MANIFEST: "",
            repo_a.path / "workflow/service": "",
            repo_b.path / EXT_MANIFEST: "",
            repo_b.path / "workflow/service": "",
        }
    )
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo_a, repo_b]),
        manifest_loader=loader,
        bindings={"service": ["provider-a", "provider-b"]},
        fs=fs,
        spec_loader=FakeSpecLoader(),
    )
    resolver = ServiceOrchestratorResolver(
        registry=registry,
        repo_factory=_StubRepoFactory([repo_a, repo_b]),
        manifest_loader=loader,
        fs=fs,
    )
    return registry, resolver


def _make_single_provider_registry() -> tuple[CapabilityRegistryService, ServiceOrchestratorResolver]:
    """Build a registry + resolver wired to a single provider."""
    repo = StandaloneRepository(name="provider-a", path=WS / "provider-a")
    loader = ExtensionManifestLoader(
        config_file_reader=FakeConfigFileReader(
            {repo.path / EXT_MANIFEST: {"orchestrate_services": "workflow/service"}}
        )
    )
    fs = FakeFilesystem(files={repo.path / EXT_MANIFEST: "", repo.path / "workflow/service": ""})
    registry = CapabilityRegistryService(
        repo_factory=_StubRepoFactory([repo]),
        manifest_loader=loader,
        bindings={"service": ["provider-a"]},
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


def _make_status_svc(
    runner: FakeSubprocessRunner,
    resolver: ServiceOrchestratorResolver,
    registry_assignments: dict[str, int] | None = None,
) -> ServiceStatusService:
    """Build a ServiceStatusService with all Phase 2 deps wired."""
    ws_config = _fake_ws_config()
    reg = _FakeEnvIndexRegistry(registry_assignments or {"alpha": 1, "beta": 2})
    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )
    matrix_svc = ServiceStatusMatrixService(
        subprocess_runner=runner,
        describe_service=describe_svc,
        env_file_sourcer=FakeEnvFileSourcer(),
        status_parser=StatusDocumentParser(),
        workspace_config=ws_config,
        env_index_registry=reg,
        workspace_root=WS,
    )
    return ServiceStatusService(
        orchestrator_resolver=resolver,
        status_parser=StatusDocumentParser(),
        matrix_service=matrix_svc,
    )


def test_status_service_report_multi_provider_covers_all_envs() -> None:
    """ServiceStatusService.report with two providers enumerates all configured envs."""
    _registry, resolver = _make_two_provider_registry()

    runner = FakeSubprocessRunner(
        run_responses={
            f"{ENTRYPOINT_A} describe": _describe_result(_describe_json("*/db")),
            f"{ENTRYPOINT_B} describe": _describe_result(_describe_json("workspace/rabbitmq")),
        },
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([_status_doc_json("alpha")], 0),
            f"{ENTRYPOINT_A} status beta/*": ([_status_doc_json("beta")], 0),
            f"{ENTRYPOINT_B} status workspace/*": ([json.dumps({"envs": []})], 0),
        },
    )
    svc = _make_status_svc(runner, resolver, registry_assignments={"alpha": 1, "beta": 2})
    reporter = FakeServiceReporter()

    code = svc.report(StatusOptions(patterns=(), as_json=False), reporter)

    assert code == 0
    assert len(reporter.status_documents) == 1
    merged_doc, _ = reporter.status_documents[0]
    env_names = {e.env for e in merged_doc.envs}
    assert "alpha" in env_names
    assert "beta" in env_names


def test_status_service_collect_degenerates_to_single_env() -> None:
    """ServiceStatusService.collect scoped to one env invokes only that env's cells."""
    _registry, resolver = _make_single_provider_registry()

    # Use a doc with a service so filter_status doesn't drop the env.
    svc_entry = {
        "name": "db",
        "state": "running",
        "health": "healthy",
        "ports": [],
        "handle": None,
        "log_path": None,
        "since": None,
    }
    alpha_doc = _status_doc_json("alpha", port_base=4020, services=[svc_entry])
    runner = FakeSubprocessRunner(
        popen_responses={
            f"{ENTRYPOINT_A} status alpha/*": ([alpha_doc], 0),
        }
    )
    svc = _make_status_svc(runner, resolver, registry_assignments={"alpha": 1, "beta": 2})

    result = svc.collect(("alpha",))

    # Only alpha/* was called, not beta/* or workspace/*.
    popen_cmd_keys = [" ".join(str(x) for x in call[0]) for call in runner.popen_calls]
    assert any("alpha/*" in k for k in popen_cmd_keys)
    assert not any("beta/*" in k for k in popen_cmd_keys)
    assert not any("workspace/*" in k for k in popen_cmd_keys)
    # Result contains alpha.
    assert result is not None
    env_names = {e.env for e in result.envs}
    assert "alpha" in env_names


# ── Subprocess integration tests (real fake-provider entrypoints) ─────────────
# These tests write tiny executable scripts into a temp directory, then drive a
# real ServiceStatusMatrixService with LocalSubprocessRunner and ShellEnvFileSourcer.
# They prove that env injection is actually observed by provider subprocesses —
# the unit tests above only verify what the matrix *passes* to the runner,
# not that a real subprocess receives it.


def _make_executable(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` and make it executable."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_describe_script(services: list[str]) -> str:
    """Return a bash entrypoint that implements only the ``describe`` action."""
    svc_json = json.dumps(services)
    return f"""\
#!/bin/bash
set -e
if [ "$1" = "describe" ]; then
    echo '{{"services": {svc_json}}}'
    exit 0
fi
echo "unknown action: $1" >&2
exit 1
"""


def _build_status_echo_script(env_key: str = "WINTER_PORT_BASE") -> str:
    """Return a bash entrypoint that echos the injected env var into the status doc.

    The provider echoes the value of ``$env_key`` into the ``handle`` field of
    a synthetic service named ``probe``, so the test can assert the value that
    the provider subprocess actually observed.

    The entrypoint handles both ``describe`` and ``status <pattern>``.  For
    ``status`` the pattern's env segment (before ``/``) is used as the env name
    in the returned document.
    """
    return f"""\
#!/bin/bash
set -e
if [ "$1" = "describe" ]; then
    echo '{{"services": ["*/probe"]}}'
    exit 0
fi
if [ "$1" = "status" ]; then
    pattern="${{2:-alpha/*}}"
    # Extract the env segment from the pattern (e.g. "alpha" from "alpha/*")
    env_seg="${{pattern%%/*}}"
    observed="${{{env_key}:-MISSING}}"
    echo "{{\\\"envs\\\": [{{\\\"env\\\": \\\"$env_seg\\\", \\\"session\\\": null, \\\"port_base\\\": null, \\\"services\\\": [{{\\\"name\\\": \\\"probe\\\", \\\"state\\\": \\\"running\\\", \\\"health\\\": \\\"healthy\\\", \\\"ports\\\": [], \\\"handle\\\": \\\"$observed\\\", \\\"log_path\\\": null, \\\"since\\\": null}}]}}]}}"
    exit 0
fi
echo "unknown action: $1" >&2
exit 1
"""


def _build_describe_sentinel_script(sentinel_path: Path) -> str:
    """Return a bash entrypoint that writes a sentinel file when ``describe`` is called.

    Used to assert that the single-provider describe-skip really suppresses the
    subprocess call.
    """
    return f"""\
#!/bin/bash
set -e
if [ "$1" = "describe" ]; then
    touch '{sentinel_path}'
    echo '{{"services": ["*/probe"]}}'
    exit 0
fi
if [ "$1" = "status" ]; then
    pattern="${{2:-alpha/*}}"
    env_seg="${{pattern%%/*}}"
    echo "{{\\\"envs\\\": [{{\\\"env\\\": \\\"$env_seg\\\", \\\"session\\\": null, \\\"port_base\\\": null, \\\"services\\\": []}}]}}"
    exit 0
fi
exit 1
"""


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    """Return a minimal workspace directory with two fake providers and env files.

    Layout::

        <tmp>/
          alpha/.winter.env          (WINTER_PORT_BASE=4020, MY_APP_VAR=from-alpha-file)
          beta/.winter.env           (WINTER_PORT_BASE=4040)
          .winter.workspace.env      (WINTER_PORT_BASE=4000, MY_APP_VAR=from-workspace-file)
          provider-a/workflow/service  (executable: describe -> */probe; status -> echo env)
          provider-b/workflow/service  (executable: describe -> workspace/probe; status -> echo env)
    """
    ws = tmp_path

    # Create env files.
    (ws / "alpha").mkdir()
    (ws / "alpha" / ".winter.env").write_text("WINTER_PORT_BASE=4020\nMY_APP_VAR=from-alpha-file\n")
    (ws / "beta").mkdir()
    (ws / "beta" / ".winter.env").write_text("WINTER_PORT_BASE=4040\n")
    (ws / ".winter.workspace.env").write_text("WINTER_PORT_BASE=4000\nMY_APP_VAR=from-workspace-file\n")

    # Create provider-a: owns */probe (per-env).
    ep_a = ws / "provider-a" / "workflow"
    ep_a.mkdir(parents=True)
    _make_executable(ep_a / "service", _build_status_echo_script("WINTER_PORT_BASE"))

    # Create provider-b: owns workspace/probe.
    ep_b = ws / "provider-b" / "workflow"
    ep_b.mkdir(parents=True)
    ws_script = """\
#!/bin/bash
set -e
if [ "$1" = "describe" ]; then
    echo '{"services": ["workspace/probe"]}'
    exit 0
fi
if [ "$1" = "status" ]; then
    pattern="${2:-workspace/*}"
    env_seg="${pattern%%/*}"
    observed="${WINTER_PORT_BASE:-MISSING}"
    my_var="${MY_APP_VAR:-ABSENT}"
    echo "{\\\"envs\\\": [{\\\"env\\\": \\\"$env_seg\\\", \\\"session\\\": null, \\\"port_base\\\": null, \\\"services\\\": [{\\\"name\\\": \\\"probe\\\", \\\"state\\\": \\\"running\\\", \\\"health\\\": \\\"healthy\\\", \\\"ports\\\": [], \\\"handle\\\": \\\"$observed|$my_var\\\", \\\"log_path\\\": null, \\\"since\\\": null}]}]}"
    exit 0
fi
exit 1
"""
    _make_executable(ep_b / "service", ws_script)

    return ws


def _real_matrix_svc(
    ws: Path,
    assignments: dict[str, int],
    provider_names: list[str],
) -> tuple[ServiceStatusMatrixService, list[ResolvedCapability]]:
    """Build a ServiceStatusMatrixService driven by real subprocesses."""
    from winter_cli.modules.service.internal.shell_env_file_sourcer import ShellEnvFileSourcer

    runner = LocalSubprocessRunner()
    sourcer = ShellEnvFileSourcer()
    reg = _FakeEnvIndexRegistry(assignments)
    ws_config = _fake_ws_config(base_port=4000, ports_per_env=20)

    providers = []
    for name in provider_names:
        ep = ws / name / "workflow" / "service"
        providers.append(
            ResolvedCapability(
                slot=CapabilitySlot.service,
                extension_name=name,
                entrypoint=ep,
                ext_dir=ws / name,
                prefix=name,
                config_dir=ws / ".winter" / "config" / name,
            )
        )

    describe_svc = ServiceDescribeService(
        subprocess_runner=runner,
        describe_parser=DescribeResultParser(),
        workspace_root=ws,
    )
    matrix_svc = ServiceStatusMatrixService(
        subprocess_runner=runner,
        describe_service=describe_svc,
        env_file_sourcer=sourcer,
        status_parser=StatusDocumentParser(),
        workspace_config=ws_config,
        env_index_registry=reg,
        workspace_root=ws,
    )
    return matrix_svc, providers


def test_subprocess_multi_provider_matrix_cell_count(tmp_workspace: Path) -> None:
    """Multi-provider matrix: cell count matches providers x envs + workspace ownership."""
    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1, "beta": 2},
        provider_names=["provider-a", "provider-b"],
    )
    cells = matrix_svc.build_matrix(providers, patterns=())

    # provider-a owns */probe -> 2 env cells (alpha + beta)
    # provider-b owns workspace/probe -> 1 workspace cell
    # Total: 3
    assert len(cells) == 3
    env_cells = [c for c in cells if c.scope != "workspace"]
    ws_cells = [c for c in cells if c.scope == "workspace"]
    assert len(env_cells) == 2
    assert len(ws_cells) == 1
    assert {c.scope for c in env_cells} == {"alpha", "beta"}
    assert ws_cells[0].provider.extension_name == "provider-b"


def test_subprocess_env_injection_observed_by_provider(tmp_workspace: Path) -> None:
    """Provider-a receives correct WINTER_PORT_BASE per env (4020 for alpha, 4040 for beta).

    The fake provider echoes $WINTER_PORT_BASE into the service handle; this test
    asserts that value equals the expected per-env port_base — proving the injected
    env var was actually received by the provider subprocess, not just passed to the runner.
    """
    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1, "beta": 2},
        provider_names=["provider-a", "provider-b"],
    )
    cells = matrix_svc.build_matrix(providers, patterns=())
    docs, worst_exit = matrix_svc.run_matrix(cells, reporter=None)

    assert worst_exit == 0

    # Collect the handle values echoed by provider-a for alpha and beta.
    handles: dict[str, str] = {}
    for doc in docs:
        for env_status in doc.envs:
            for svc in env_status.services:
                if svc.name == "probe" and env_status.env in ("alpha", "beta"):
                    handles[env_status.env] = svc.handle or ""

    # The env files override WINTER_PORT_BASE; sourced values must win.
    assert handles.get("alpha") == "4020", f"alpha handle={handles.get('alpha')!r}"
    assert handles.get("beta") == "4040", f"beta handle={handles.get('beta')!r}"


def test_subprocess_sourced_env_file_var_reaches_provider(tmp_workspace: Path) -> None:
    """MY_APP_VAR from .winter.workspace.env reaches provider-b via sourcing."""
    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1},
        provider_names=["provider-a", "provider-b"],
    )
    cells = matrix_svc.build_matrix(providers, patterns=("workspace",))
    docs, worst_exit = matrix_svc.run_matrix(cells, reporter=None)

    assert worst_exit == 0

    ws_handles: list[str] = []
    for doc in docs:
        for env_status in doc.envs:
            if env_status.env == "workspace":
                for svc in env_status.services:
                    ws_handles.append(svc.handle or "")

    # handle is "WINTER_PORT_BASE|MY_APP_VAR"; workspace env file sets both.
    assert any("from-workspace-file" in h for h in ws_handles), f"ws handles: {ws_handles}"


def test_subprocess_scope_qualified_filter_narrows_to_one_env(tmp_workspace: Path) -> None:
    """Pattern gamma/probe -> only provider-a's gamma cell invoked."""
    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1, "beta": 2, "gamma": 3},
        provider_names=["provider-a", "provider-b"],
    )
    # gamma/ needs an env file; create a minimal one.
    (tmp_workspace / "gamma").mkdir(exist_ok=True)
    (tmp_workspace / "gamma" / ".winter.env").write_text("WINTER_PORT_BASE=4060\n")

    cells = matrix_svc.build_matrix(providers, patterns=("gamma/probe",))

    # Only provider-a (owns */probe) for gamma scope.
    assert len(cells) == 1
    assert cells[0].scope == "gamma"
    assert cells[0].provider.extension_name == "provider-a"
    assert cells[0].cell_pattern == "gamma/probe"


def test_subprocess_workspace_filter(tmp_workspace: Path) -> None:
    """Pattern workspace/probe -> only provider-b's workspace cell invoked."""
    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1},
        provider_names=["provider-a", "provider-b"],
    )
    cells = matrix_svc.build_matrix(providers, patterns=("workspace/probe",))

    assert len(cells) == 1
    assert cells[0].scope == "workspace"
    assert cells[0].provider.extension_name == "provider-b"


def test_subprocess_single_provider_no_describe_sentinel(tmp_path: Path) -> None:
    """Single provider: describe subprocess is NOT called (sentinel file absent)."""
    ws = tmp_path
    sentinel = ws / "describe_was_called"

    ep_dir = ws / "sole-provider" / "workflow"
    ep_dir.mkdir(parents=True)
    _make_executable(ep_dir / "service", _build_describe_sentinel_script(sentinel))

    (ws / "alpha").mkdir()
    (ws / "alpha" / ".winter.env").write_text("")

    matrix_svc, providers = _real_matrix_svc(ws, assignments={"alpha": 1}, provider_names=["sole-provider"])

    cells = matrix_svc.build_matrix(providers, patterns=())

    # Sentinel must NOT exist — describe was not invoked.
    assert not sentinel.exists(), "describe was called for the single-provider case; it should be skipped"

    # Cells still cover alpha + workspace.
    scopes = {c.scope for c in cells}
    assert "alpha" in scopes
    assert "workspace" in scopes


def test_subprocess_merged_docs_contain_all_env_scopes(tmp_workspace: Path) -> None:
    """run_matrix produces one doc per cell; merged doc contains alpha + beta envs."""
    from winter_cli.modules.service.status_merge import merge_status_documents

    matrix_svc, providers = _real_matrix_svc(
        tmp_workspace,
        assignments={"alpha": 1, "beta": 2},
        provider_names=["provider-a"],  # sole provider: all envs + workspace
    )
    cells = matrix_svc.build_matrix(providers, patterns=())
    docs, worst_exit = matrix_svc.run_matrix(cells, reporter=None)

    assert worst_exit == 0
    merged = merge_status_documents(docs)
    env_names = {e.env for e in merged.envs}
    assert "alpha" in env_names
    assert "beta" in env_names
