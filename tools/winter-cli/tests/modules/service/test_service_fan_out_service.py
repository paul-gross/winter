"""Tests for ServiceFanOutService — cell-based up/down fan-out (no readiness gate).

Coverage:
- Single-cell up: calls the cell's provider up, returns 0.
- Single-cell up: propagates non-zero exit code.
- Single-cell down: calls the cell's provider down, returns 0.
- Single-cell down: propagates non-zero exit code.
- Two-cell up (single scope, two providers): calls both cells' up in forward order
  with NO status poll between them (proves the gate is gone).
- Two-cell up: aborts on first cell failure; second cell never called.
- Two-cell down: calls both cells' down (best-effort).
- Two-cell down: continues past failure; returns first non-zero.
- Two-cell down: returns 0 when all succeed.
- Multi-scope up/down: two cells targeting different scopes both dispatch, in order.
- Env vars are injected correctly for each cell.
- Provisioned scope env vars (including WINTER_SERVICE_PREFIX) are merged into
  both up and down subprocess env when an env_provisioner is present, computed
  once per unique scope (cached across cells sharing a scope).
- Cell positional (bare scope vs scope-qualified pattern) is forwarded verbatim
  as the single positional argv token.
- WINTER_SERVICE_TIMEOUT is injected on up (default and caller-supplied), never on down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeCommandEntryRunner, FakeServiceReporter, FakeSubprocessRunner
from winter_cli.config.models import (
    EnvCommandEntry,
    EnvVarBands,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    WorkspaceConfig,
)
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.service_fan_out_service import FanOutCell, ServiceFanOutService
from winter_cli.modules.workspace.env_band_resolver_service import COMMAND_PLACEHOLDER, EnvBandResolverService
from winter_cli.modules.workspace.env_provisioner import EnvProvisionerService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.subprocess_command_entry_runner import SubprocessCommandEntryRunner
from winter_cli.modules.workspace.models.domain_model import RepoError

WS = Path("/ws")
EXT_A = WS / "provider-a"
EXT_B = WS / "provider-b"
ENTRYPOINT_A = EXT_A / "workflow/service"
ENTRYPOINT_B = EXT_B / "workflow/service"

_EP_A = str(ENTRYPOINT_A)
_EP_B = str(ENTRYPOINT_B)

_UP_A = f"{_EP_A} up alpha"
_UP_B = f"{_EP_B} up alpha"
_DOWN_A = f"{_EP_A} down alpha"
_DOWN_B = f"{_EP_B} down alpha"


# ── helpers ───────────────────────────────────────────────────────────────────


def _provider(name: str, entrypoint: Path, ext_dir: Path) -> ResolvedCapability:
    return ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name=name,
        entrypoint=entrypoint,
        ext_dir=ext_dir,
        prefix=name,
        config_dir=WS / ".winter" / "config" / name,
    )


def _pa() -> ResolvedCapability:
    return _provider("provider-a", ENTRYPOINT_A, EXT_A)


def _pb() -> ResolvedCapability:
    return _provider("provider-b", ENTRYPOINT_B, EXT_B)


def _cell(provider: ResolvedCapability, scope: str = "alpha", positional: str | None = None) -> FanOutCell:
    return FanOutCell(provider=provider, scope=scope, positional=positional if positional is not None else scope)


def _make_fan_out(runner: FakeSubprocessRunner) -> ServiceFanOutService:
    return ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
    )


# ── single-cell up ────────────────────────────────────────────────────────────


def test_single_cell_up_calls_up_returns_zero() -> None:
    """Single-cell up: calls the provider's up and returns 0."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.up([_cell(_pa())])

    assert code == 0
    assert runner.call_calls == [([_EP_A, "up", "alpha"], WS)]
    # No status poll (no gate).
    assert runner.run_calls == []


def test_single_cell_up_propagates_nonzero_exit() -> None:
    """Single-cell up: propagates non-zero exit code."""
    runner = FakeSubprocessRunner(call_responses={_UP_A: 5})
    svc = _make_fan_out(runner)

    code = svc.up([_cell(_pa())])

    assert code == 5


# ── single-cell down ──────────────────────────────────────────────────────────


def test_single_cell_down_calls_down_returns_zero() -> None:
    """Single-cell down: calls the provider's down and returns 0."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa())])

    assert code == 0
    assert runner.call_calls == [([_EP_A, "down", "alpha"], WS)]
    assert runner.run_calls == []


def test_single_cell_down_propagates_nonzero_exit() -> None:
    """Single-cell down: propagates non-zero exit code."""
    runner = FakeSubprocessRunner(call_responses={_DOWN_A: 3})
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa())])

    assert code == 3


# ── two-cell up (no gate) ─────────────────────────────────────────────────────


def test_two_cell_up_calls_both_in_forward_order_no_status_poll() -> None:
    """Two cells (same scope, two providers) up: forward order, NO status poll between.

    This is the primary proof that the readiness gate is gone: no status call
    sits between the two up calls.
    """
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.up([_cell(_pa()), _cell(_pb())])

    assert code == 0

    # Exact call sequence: up-a then up-b, nothing else.
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (_EP_A, "up", "alpha"),
        (_EP_B, "up", "alpha"),
    ]

    # No run() calls at all — proves no status poll between the two ups.
    assert runner.run_calls == []


def test_two_cell_up_aborts_on_first_failure_second_never_called() -> None:
    """Two-cell up: if the first cell's up exits non-zero, the second is never called."""
    runner = FakeSubprocessRunner(call_responses={_UP_A: 7})
    svc = _make_fan_out(runner)

    code = svc.up([_cell(_pa()), _cell(_pb())])

    assert code == 7
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    # Only provider-a's up was called.
    assert (_EP_A, "up", "alpha") in call_cmds
    assert (_EP_B, "up", "alpha") not in call_cmds
    # No status poll.
    assert runner.run_calls == []


# ── two-cell down (best-effort) ──────────────────────────────────────────────


def test_two_cell_down_calls_both_providers() -> None:
    """Two-cell down: calls both providers' down."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa()), _cell(_pb())])

    assert code == 0
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_B, "down", "alpha") in call_cmds


def test_two_cell_down_best_effort_continues_on_failure() -> None:
    """Down is best-effort: continues past cell failure; returns first non-zero."""
    # Provider A fails.
    runner = FakeSubprocessRunner(call_responses={_DOWN_A: 4})
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa()), _cell(_pb())])

    # Both down calls were made (best-effort continues).
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_B, "down", "alpha") in call_cmds
    # First non-zero returned.
    assert code == 4


def test_two_cell_down_all_succeed_returns_zero() -> None:
    """Down returns 0 when all cells succeed."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa()), _cell(_pb())])

    assert code == 0


# ── multi-scope fan-out ───────────────────────────────────────────────────────


def test_up_multi_scope_dispatches_each_scope_in_order() -> None:
    """Two cells targeting different scopes (same provider) both dispatch, in order."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.up([_cell(_pa(), scope="alpha"), _cell(_pa(), scope="beta")])

    assert code == 0
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (_EP_A, "up", "alpha"),
        (_EP_A, "up", "beta"),
    ]


def test_down_multi_scope_best_effort_across_scopes() -> None:
    """Down across multiple scopes: continues past a failure in one scope."""
    runner = FakeSubprocessRunner(call_responses={f"{_EP_A} down alpha": 6})
    svc = _make_fan_out(runner)

    code = svc.down([_cell(_pa(), scope="alpha"), _cell(_pa(), scope="beta")])

    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_A, "down", "beta") in call_cmds
    assert code == 6


# ── cell positional forwarding ────────────────────────────────────────────────


def test_up_forwards_scope_qualified_positional_verbatim() -> None:
    """A cell with a scope-qualified positional (real service filter) forwards it verbatim."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up([_cell(_pa(), scope="alpha", positional="alpha/api")])

    assert runner.call_calls == [([_EP_A, "up", "alpha/api"], WS)]


# ── env var injection ─────────────────────────────────────────────────────────


def test_up_injects_provider_env_vars() -> None:
    """Fan-out up injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, WINTER_SERVICE_PREFIX per cell."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up([_cell(_pa())])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert call_env["WINTER_EXT_DIR"] == str(EXT_A)
    assert call_env["WINTER_EXT_PREFIX"] == "provider-a"
    assert call_env["WINTER_SERVICE_PREFIX"] == "winter"


def test_down_injects_provider_env_vars() -> None:
    """Fan-out down injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX, WINTER_SERVICE_PREFIX per cell."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.down([_cell(_pb())])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert call_env["WINTER_EXT_DIR"] == str(EXT_B)
    assert call_env["WINTER_EXT_PREFIX"] == "provider-b"
    assert call_env["WINTER_SERVICE_PREFIX"] == "winter"


def test_up_injects_provisioned_env_vars_when_provisioner_present() -> None:
    """When an env_provisioner is present, its computed vars are merged into the subprocess env."""

    class _FakeProvisioner:
        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            return {
                "WINTER_ENV": scope,
                "WINTER_PORT_BASE": "4060",
                "WINTER_SERVICE_PREFIX": "myproj",
                "DATABASE_URL": f"postgres://localhost/myapp_{scope}",
            }

    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=_FakeProvisioner(),
    )

    svc.up([_cell(_pa())])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_ENV"] == "alpha"
    assert call_env["WINTER_PORT_BASE"] == "4060"
    assert call_env["WINTER_SERVICE_PREFIX"] == "myproj"
    assert call_env["DATABASE_URL"] == "postgres://localhost/myapp_alpha"


def test_down_injects_provisioned_env_vars_when_provisioner_present() -> None:
    """When an env_provisioner is present, its computed vars are merged into the down subprocess env."""

    class _FakeProvisioner:
        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            return {
                "WINTER_ENV": scope,
                "WINTER_PORT_BASE": "4060",
                "WINTER_SERVICE_PREFIX": "myproj",
                "DATABASE_URL": f"postgres://localhost/myapp_{scope}",
            }

    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=_FakeProvisioner(),
    )

    svc.down([_cell(_pb())])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_ENV"] == "alpha"
    assert call_env["WINTER_PORT_BASE"] == "4060"
    assert call_env["WINTER_SERVICE_PREFIX"] == "myproj"
    assert call_env["DATABASE_URL"] == "postgres://localhost/myapp_alpha"


def test_up_injects_default_timeout_env_var() -> None:
    """Without an explicit timeout_s, up injects the DEFAULT_WAIT_TIMEOUT_S default."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up([_cell(_pa())])

    assert runner.call_envs[0]["WINTER_SERVICE_TIMEOUT"] == "120.0"


def test_up_injects_custom_timeout_env_var() -> None:
    """A caller-supplied timeout_s is forwarded verbatim as a plain float string."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up([_cell(_pa())], 45.0)

    assert runner.call_envs[0]["WINTER_SERVICE_TIMEOUT"] == "45.0"


def test_down_does_not_inject_timeout_env_var() -> None:
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.down([_cell(_pa())])

    assert "WINTER_SERVICE_TIMEOUT" not in runner.call_envs[0]


def test_up_no_provisioned_env_vars_when_provisioner_absent() -> None:
    """Without an env_provisioner, scope vars are not injected into the subprocess env."""
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up([_cell(_pa())])

    call_env = runner.call_envs[0]
    assert "WINTER_ENV" not in call_env


def test_up_provisions_each_unique_scope_once() -> None:
    """A scope shared by multiple cells (multi-provider) is provisioned exactly once."""

    class _CountingProvisioner:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.resolve_commands_calls: list[bool] = []

        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            self.calls.append(scope)
            self.resolve_commands_calls.append(resolve_commands)
            return {"WINTER_ENV": scope}

    provisioner = _CountingProvisioner()
    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
    )

    svc.up([_cell(_pa(), scope="alpha"), _cell(_pb(), scope="alpha"), _cell(_pa(), scope="beta")])

    assert provisioner.calls == ["alpha", "beta"]
    # up() gates every scope's compute() with resolve_commands=True.
    assert provisioner.resolve_commands_calls == [True, True]


class _InMemoryEnvIndexRegistry:
    """Minimal IEnvIndexRegistry fake for wiring a real EnvProvisionerService."""

    def __init__(self, assignments: dict[str, int]) -> None:
        self._data = dict(assignments)

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


def _config_with_bands(env_bands: EnvVarBands) -> WorkspaceConfig:
    """A minimal WorkspaceConfig carrying *env_bands*."""
    return WorkspaceConfig(
        workspace_root=WS,
        main_branch="main",
        base_port=4000,
        ports_per_env=20,
        singleton_repos=[SingletonRepository(name="ws", type=SingletonType.workspace)],
        project_repos=[ProjectRepositoryConfig(name="demo", url="git@example.com:demo.git")],
        env_bands=env_bands,
    )


def _real_provisioner(command_runner: FakeCommandEntryRunner) -> EnvProvisionerService:
    """A real EnvProvisionerService whose feature band declares one command entry.

    Used to pin the actual execution count a regression could silently break —
    a fake provisioner recording resolve_commands values cannot detect a change
    that re-resolves per cell instead of per unique scope.
    """
    config = _config_with_bands(EnvVarBands(feature={"SECRET": EnvCommandEntry(command="echo hi")}))
    registry = _InMemoryEnvIndexRegistry({"alpha": 1, "beta": 2})
    return EnvProvisionerService(
        config=config, registry=registry, band_resolver=EnvBandResolverService(runner=command_runner)
    )


def test_up_runs_a_command_entry_exactly_once_per_unique_scope() -> None:
    """A command entry declared in the feature band runs once per unique scope on up(), not once per cell.

    Pins the real execution count against a real ICommandEntryRunner: two
    cells sharing scope "alpha" must trigger exactly one command execution —
    a regression that re-resolved per cell instead of per cached scope would
    make this 2.
    """
    command_runner = FakeCommandEntryRunner({"echo hi": "hunter2"})
    provisioner = _real_provisioner(command_runner)
    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
    )

    svc.up([_cell(_pa(), scope="alpha"), _cell(_pb(), scope="alpha")])

    assert len(command_runner.calls) == 1
    assert runner.call_envs[0]["SECRET"] == "hunter2"
    assert runner.call_envs[1]["SECRET"] == "hunter2"


def test_down_never_runs_a_command_entry() -> None:
    """down() gates resolve_commands=False: a command entry never executes on teardown."""
    command_runner = FakeCommandEntryRunner({"echo hi": "hunter2"})
    provisioner = _real_provisioner(command_runner)
    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
    )

    svc.down([_cell(_pa(), scope="alpha"), _cell(_pb(), scope="alpha")])

    assert command_runner.calls == []
    assert runner.call_envs[0]["SECRET"] == COMMAND_PLACEHOLDER


def test_up_propagates_a_command_failure_instead_of_degrading() -> None:
    """A failed command entry under ``up`` surfaces as RepoError — it never degrades to no injection.

    ``provision_scope_env`` catches ``ValueError`` only, so a template error
    keeps its best-effort degradation while a command failure propagates: the
    fail-loud contract command entries carry. Pins that the catch is not
    broadened — a wider ``except`` would swallow this and silently start the
    provider with the command's value missing.
    """
    failure = RepoError(
        "env.feature.vars key 'SECRET': command `echo hi` failed",
        subcommand="hi",
        cwd=str(WS),
        exit_code=3,
        program="echo",
    )
    command_runner = FakeCommandEntryRunner({"echo hi": failure})
    provisioner = _real_provisioner(command_runner)
    runner = FakeSubprocessRunner()
    reporter = FakeServiceReporter()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
        reporter=reporter,
    )

    with pytest.raises(RepoError) as excinfo:
        svc.up([_cell(_pa(), scope="alpha")])

    assert "SECRET" in str(excinfo.value)
    # Not degraded: no reporter diagnostic, and the provider never ran.
    assert reporter.env_provision_error_calls == []
    assert runner.call_calls == []


def test_up_propagates_a_malformed_command_instead_of_degrading() -> None:
    """M1: a `command` entry with a quoting error surfaces as `RepoError` under ``up``, not `{}`.

    Uses the real `SubprocessCommandEntryRunner` — a fake never exercises
    `shlex.split` at all, so this is the only level that can catch a
    tokenizer failure escaping as a bare `ValueError`, which
    `provision_scope_env` would silently degrade to no env injected (and
    `up` would then start the provider with none of its env at all).
    """
    config = _config_with_bands(EnvVarBands(feature={"SECRET": EnvCommandEntry(command='vals get "unbalanced')}))
    provisioner = EnvProvisionerService(
        config=config,
        registry=_InMemoryEnvIndexRegistry({"alpha": 1}),
        band_resolver=EnvBandResolverService(
            runner=SubprocessCommandEntryRunner(workspace_root=WS, error_factory=RepoErrorFactory())
        ),
    )
    runner = FakeSubprocessRunner()
    reporter = FakeServiceReporter()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
        reporter=reporter,
    )

    with pytest.raises(RepoError) as excinfo:
        svc.up([_cell(_pa(), scope="alpha")])

    assert "SECRET" in str(excinfo.value)
    # Not degraded: no reporter diagnostic, and the provider never ran.
    assert reporter.env_provision_error_calls == []
    assert runner.call_calls == []


def test_status_style_template_error_still_degrades_on_up() -> None:
    """A ValueError from a bad template keeps degrading to no injection, next to the RepoError case above."""
    config = _config_with_bands(EnvVarBands(feature={"BAD": "${NOPE}"}))
    provisioner = EnvProvisionerService(
        config=config,
        registry=_InMemoryEnvIndexRegistry({"alpha": 1}),
        band_resolver=EnvBandResolverService(runner=FakeCommandEntryRunner()),
    )
    runner = FakeSubprocessRunner()
    reporter = FakeServiceReporter()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=provisioner,
        reporter=reporter,
    )

    svc.up([_cell(_pa(), scope="alpha")])

    assert [scope for scope, _ in reporter.env_provision_error_calls] == ["alpha"]
    assert "BAD" not in runner.call_envs[0]


# ── env provision error resilience ───────────────────────────────────────────


# ── per-scope band selection regression ──────────────────────────────────────


def test_up_workspace_scope_injects_workspace_band_only() -> None:
    """Workspace scope: provisioner's output has workspace-band key but NOT feature-only key.

    Proves that the fan-out consumer passes the scope verbatim to compute()
    and injects the result without any second band-selection code path.
    A provisioner simulating [env.workspace.vars] SHARED + [env.feature.vars] FEAT_ONLY
    returns only SHARED at workspace scope; FEAT_ONLY must not appear in the env.
    """

    class _BandProvisioner:
        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            if scope == "workspace":
                return {"SHARED": "ws_val"}
            return {"SHARED": "feat_override", "FEAT_ONLY": "feat_val"}

    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=_BandProvisioner(),
    )

    svc.up([_cell(_pa(), scope="workspace", positional="workspace")])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["SHARED"] == "ws_val"
    assert "FEAT_ONLY" not in call_env


def test_up_feature_scope_injects_both_bands_feature_wins_collision() -> None:
    """Feature scope: provisioner's output has both workspace-band and feature-band keys.

    Proves that the fan-out consumer passes the scope verbatim to compute()
    and injects the result without any second band-selection code path.
    A provisioner simulating [env.workspace.vars] SHARED + [env.feature.vars] SHARED/FEAT_ONLY
    returns both SHARED (feature value wins collision) and FEAT_ONLY at feature scope.
    """

    class _BandProvisioner:
        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            if scope == "workspace":
                return {"SHARED": "ws_val"}
            return {"SHARED": "feat_override", "FEAT_ONLY": "feat_val"}

    runner = FakeSubprocessRunner()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=_BandProvisioner(),
    )

    svc.up([_cell(_pa())])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["SHARED"] == "feat_override"
    assert call_env["FEAT_ONLY"] == "feat_val"


def test_provision_error_does_not_raise_on_up_or_down() -> None:
    """A ValueError from the provisioner degrades to no-injection; up/down do not raise."""

    class _ErrorProvisioner:
        def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
            raise ValueError(f"bad template for {scope}")

    class _FakeReporter:
        def __init__(self) -> None:
            self.provision_errors: list[tuple[str, str]] = []

        def env_provision_error(self, scope: str, detail: str) -> None:
            self.provision_errors.append((scope, detail))

    runner = FakeSubprocessRunner()
    reporter = _FakeReporter()
    svc = ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
        service_prefix="winter",
        env_provisioner=_ErrorProvisioner(),
        reporter=reporter,  # type: ignore[arg-type]
    )

    # up must not raise; provider still runs (degraded to no injection)
    code_up = svc.up([_cell(_pa())])
    assert code_up == 0

    # down must not raise; provider still runs
    code_down = svc.down([_cell(_pa())])
    assert code_down == 0

    # reporter received env_provision_error for each call (one up + one down)
    assert len(reporter.provision_errors) == 2
    assert all(scope == "alpha" for scope, _ in reporter.provision_errors)

    # The provider did run despite the error (both up and down invocations)
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "up", "alpha") in call_cmds
    assert (_EP_A, "down", "alpha") in call_cmds
