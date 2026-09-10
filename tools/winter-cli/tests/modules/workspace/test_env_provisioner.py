"""Tests for EnvProvisionerService — the single source of truth for runtime env maps.

Covers:
- Feature-env scope: WINTER_ENV / WINTER_ENV_INDEX / WINTER_PORT_BASE /
  WINTER_WORKSPACE_PORT_BASE are computed from the registry-assigned index.
- Workspace scope: index 0, port_base_for_index(0).
- Band selection: workspace scope → workspace band only; feature scope → union
  with feature winning collisions; workspace keys visible to feature templates.
- Per-env override bands ([env.<name>.vars]): win over the feature and workspace
  bands for their own env, never reach a sibling, and are inert for an env that
  does not exist.
- Env-band rendering: ${NAME}, ${NAME+N} expansion, sibling references.
- Env-band error cases: undefined variable, unsupported token, non-integer +N.
- C4 invariant: workspace-band entries resolve identically at workspace and feature scope.
- ``resolve_commands`` is forwarded verbatim from ``compute`` to the resolver
  (fixpoint / merge-policy / execution behavior itself is
  ``test_env_band_resolver_service.py``'s job).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeCommandEntryRunner
from winter_cli.config.models import (
    EnvBandValue,
    EnvCommandEntry,
    EnvVarBands,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.env_band_resolver_service import COMMAND_PLACEHOLDER, EnvBandResolverService
from winter_cli.modules.workspace.env_provisioner import EnvProvisionerService

WORKSPACE_ROOT = Path("/ws")


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------


class _InMemoryRegistry:
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


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _config(
    base_port: int = 4000,
    ports_per_env: int = 20,
    workspace_vars: dict[str, EnvBandValue] | None = None,
    feature_vars: dict[str, EnvBandValue] | None = None,
    named_vars: dict[str, dict[str, EnvBandValue]] | None = None,
    service_prefix: str | None = None,
) -> WorkspaceConfig:
    kwargs: dict = {
        "workspace_root": WORKSPACE_ROOT,
        "main_branch": "main",
        "base_port": base_port,
        "ports_per_env": ports_per_env,
        "singleton_repos": [SingletonRepository(name="ws", type=SingletonType.workspace)],
        "project_repos": [ProjectRepositoryConfig(name="demo", url="git@example.com:demo.git")],
    }
    if workspace_vars is not None or feature_vars is not None or named_vars is not None:
        kwargs["env_bands"] = EnvVarBands(
            workspace=workspace_vars or {},
            feature=feature_vars or {},
            named=named_vars or {},
        )
    if service_prefix is not None:
        kwargs["service_prefix"] = service_prefix
    return WorkspaceConfig(**kwargs)


def _svc(
    assignments: dict[str, int] | None = None,
    base_port: int = 4000,
    ports_per_env: int = 20,
    workspace_vars: dict[str, EnvBandValue] | None = None,
    feature_vars: dict[str, EnvBandValue] | None = None,
    named_vars: dict[str, dict[str, EnvBandValue]] | None = None,
    service_prefix: str | None = None,
    runner: FakeCommandEntryRunner | None = None,
) -> EnvProvisionerService:
    cfg = _config(
        base_port=base_port,
        ports_per_env=ports_per_env,
        workspace_vars=workspace_vars,
        feature_vars=feature_vars,
        named_vars=named_vars,
        service_prefix=service_prefix,
    )
    reg = _InMemoryRegistry(assignments)
    return EnvProvisionerService(
        config=cfg, registry=reg, band_resolver=EnvBandResolverService(runner=runner or FakeCommandEntryRunner())
    )


# ---------------------------------------------------------------------------
# Feature-env scope
# ---------------------------------------------------------------------------


class TestFeatureEnvScope:
    def test_winter_env_is_scope_name(self) -> None:
        """WINTER_ENV equals the scope name passed to compute()."""
        result = _svc(assignments={"alpha": 1}).compute("alpha", resolve_commands=False)
        assert result["WINTER_ENV"] == "alpha"

    def test_winter_env_index_from_registry(self) -> None:
        """WINTER_ENV_INDEX matches the registry-assigned index."""
        result = _svc(assignments={"alpha": 1}).compute("alpha", resolve_commands=False)
        assert result["WINTER_ENV_INDEX"] == "1"

    def test_winter_port_base_alpha(self) -> None:
        """WINTER_PORT_BASE is base_port + index * ports_per_env for alpha (index 1)."""
        result = _svc(assignments={"alpha": 1}, base_port=4000, ports_per_env=20).compute(
            "alpha", resolve_commands=False
        )
        assert result["WINTER_PORT_BASE"] == "4020"  # 4000 + 1 * 20

    def test_winter_port_base_beta(self) -> None:
        """WINTER_PORT_BASE is correct for beta (index 2)."""
        result = _svc(assignments={"beta": 2}, base_port=4000, ports_per_env=20).compute("beta", resolve_commands=False)
        assert result["WINTER_PORT_BASE"] == "4040"  # 4000 + 2 * 20

    def test_winter_workspace_port_base_is_index_zero(self) -> None:
        """WINTER_WORKSPACE_PORT_BASE is always port_base_for_index(0) = base_port."""
        result = _svc(assignments={"alpha": 1}, base_port=4000, ports_per_env=20).compute(
            "alpha", resolve_commands=False
        )
        assert result["WINTER_WORKSPACE_PORT_BASE"] == "4000"

    def test_winter_service_prefix_matches_config(self) -> None:
        """WINTER_SERVICE_PREFIX equals the resolved config.service_prefix for a feature scope."""
        result = _svc(assignments={"alpha": 1}, service_prefix="myproj").compute("alpha", resolve_commands=False)
        assert result["WINTER_SERVICE_PREFIX"] == "myproj"

    def test_persisted_index_used_over_formula(self) -> None:
        """A non-alias env with a persisted index uses that index, not the hash formula."""
        # "myenv" is not in env_aliases; persist index 15 out-of-band.
        result = _svc(assignments={"myenv": 15}, base_port=4000, ports_per_env=20).compute(
            "myenv", resolve_commands=False
        )
        assert result["WINTER_ENV_INDEX"] == "15"
        assert result["WINTER_PORT_BASE"] == "4300"  # 4000 + 15 * 20


# ---------------------------------------------------------------------------
# Workspace scope
# ---------------------------------------------------------------------------


class TestWorkspaceScope:
    def test_winter_env_is_workspace(self) -> None:
        result = _svc().compute("workspace", resolve_commands=False)
        assert result["WINTER_ENV"] == "workspace"

    def test_winter_env_index_is_zero(self) -> None:
        result = _svc().compute("workspace", resolve_commands=False)
        assert result["WINTER_ENV_INDEX"] == "0"

    def test_winter_workspace_port_base_is_index_zero(self) -> None:
        """WINTER_WORKSPACE_PORT_BASE is base_port for workspace scope (index 0)."""
        result = _svc(base_port=4000, ports_per_env=20).compute("workspace", resolve_commands=False)
        assert result["WINTER_WORKSPACE_PORT_BASE"] == "4000"

    def test_winter_port_base_not_emitted_for_workspace(self) -> None:
        """WINTER_PORT_BASE is NOT in the workspace scope result — workspace only gets WINTER_WORKSPACE_PORT_BASE."""
        result = _svc(base_port=4000, ports_per_env=20).compute("workspace", resolve_commands=False)
        assert "WINTER_PORT_BASE" not in result

    def test_winter_service_prefix_matches_config(self) -> None:
        """WINTER_SERVICE_PREFIX equals the resolved config.service_prefix for the workspace scope."""
        result = _svc(service_prefix="myproj").compute("workspace", resolve_commands=False)
        assert result["WINTER_SERVICE_PREFIX"] == "myproj"


# ---------------------------------------------------------------------------
# Band selection — workspace scope
# ---------------------------------------------------------------------------


class TestWorkspaceBandSelection:
    def test_workspace_scope_renders_only_workspace_band(self) -> None:
        """Workspace scope: workspace-band key present, feature-band key absent."""
        result = _svc(
            workspace_vars={"SHARED": "ws-only"},
            feature_vars={"FEAT": "feat-only"},
        ).compute("workspace", resolve_commands=False)
        assert result["SHARED"] == "ws-only"
        assert "FEAT" not in result

    def test_workspace_scope_omits_winter_port_base(self) -> None:
        """Workspace scope still does not emit WINTER_PORT_BASE even with workspace vars."""
        result = _svc(
            workspace_vars={"WS_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        ).compute("workspace", resolve_commands=False)
        assert "WINTER_PORT_BASE" not in result
        assert result["WS_PORT"] == "4001"

    def test_workspace_band_winter_port_base_raises_at_workspace_scope(self) -> None:
        """Workspace-band template using ${WINTER_PORT_BASE+N} raises ValueError at workspace scope.

        WINTER_PORT_BASE is never in the workspace-band template scope (it is not in
        the workspace result, and no alias is injected).  Use
        ${WINTER_WORKSPACE_PORT_BASE+N} for workspace-relative port references.
        """
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _svc(
                base_port=4000,
                workspace_vars={"WS_PORT": "${WINTER_PORT_BASE+1}"},
            ).compute("workspace", resolve_commands=False)

    def test_workspace_scope_empty_bands_returns_base_vars_only(self) -> None:
        """Absent bands for workspace scope: only the four WINTER_* base vars."""
        result = _svc().compute("workspace", resolve_commands=False)
        assert set(result.keys()) == {
            "WINTER_ENV",
            "WINTER_ENV_INDEX",
            "WINTER_WORKSPACE_PORT_BASE",
            "WINTER_SERVICE_PREFIX",
        }


# ---------------------------------------------------------------------------
# C4 invariant — workspace-band consistency across scopes
# ---------------------------------------------------------------------------


class TestWorkspaceBandPortBaseInvariant:
    """Workspace-band entries must resolve identically at workspace and feature scope."""

    def test_workspace_band_workspace_port_base_same_at_both_scopes(self) -> None:
        """${WINTER_WORKSPACE_PORT_BASE+N} in workspace band resolves to the same value at both scopes."""
        svc = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            workspace_vars={"SHARED_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        )
        ws_result = svc.compute("workspace", resolve_commands=False)
        feat_result = svc.compute("alpha", resolve_commands=False)
        # 4000 + 1 = 4001 in both cases — identical regardless of scope
        assert ws_result["SHARED_PORT"] == "4001"
        assert feat_result["SHARED_PORT"] == "4001"

    def test_workspace_band_winter_port_base_raises_at_feature_scope(self) -> None:
        """Workspace-band template using ${WINTER_PORT_BASE+N} raises ValueError at feature scope.

        WINTER_PORT_BASE is excluded from the workspace-band template scope even when
        rendering inside a feature-scope compute() call, so the error is the same as
        at workspace scope.
        """
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _svc(
                assignments={"alpha": 1},
                base_port=4000,
                workspace_vars={"WS_PORT": "${WINTER_PORT_BASE+1}"},
            ).compute("alpha", resolve_commands=False)

    def test_feature_band_winter_port_base_resolves_to_feature_base(self) -> None:
        """${WINTER_PORT_BASE+N} in the feature band still resolves to the feature's port base."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"APP_PORT": "${WINTER_PORT_BASE+5}"},
        ).compute("alpha", resolve_commands=False)
        # alpha index 1 → 4020 + 5 = 4025
        assert result["APP_PORT"] == "4025"


# ---------------------------------------------------------------------------
# Band selection — feature scope
# ---------------------------------------------------------------------------


class TestFeatureBandSelection:
    def test_feature_scope_includes_workspace_band_key(self) -> None:
        """Feature scope inherits workspace-band entries."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"SHARED": "ws-value"},
        ).compute("alpha", resolve_commands=False)
        assert result["SHARED"] == "ws-value"

    def test_feature_scope_includes_feature_band_key(self) -> None:
        """Feature scope includes feature-band entries."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"FEAT": "feat-value"},
        ).compute("alpha", resolve_commands=False)
        assert result["FEAT"] == "feat-value"

    def test_feature_wins_on_key_collision(self) -> None:
        """When the same key appears in both bands, the feature value wins."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"COMMON": "from-workspace"},
            feature_vars={"COMMON": "from-feature"},
        ).compute("alpha", resolve_commands=False)
        assert result["COMMON"] == "from-feature"

    def test_workspace_scope_gets_workspace_value_on_same_key(self) -> None:
        """Workspace scope still sees the workspace value for the colliding key."""
        result = _svc(
            workspace_vars={"COMMON": "from-workspace"},
            feature_vars={"COMMON": "from-feature"},
        ).compute("workspace", resolve_commands=False)
        assert result["COMMON"] == "from-workspace"

    def test_feature_template_references_workspace_band_key(self) -> None:
        """A feature-band template can reference a workspace-band key already rendered."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"DB_HOST": "db.example.com"},
            feature_vars={"DB_URL": "postgres://${DB_HOST}/mydb"},
        ).compute("alpha", resolve_commands=False)
        assert result["DB_HOST"] == "db.example.com"
        assert result["DB_URL"] == "postgres://db.example.com/mydb"

    def test_feature_scope_union_both_bands(self) -> None:
        """Feature scope output contains keys from both bands."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"WS_KEY": "ws-val"},
            feature_vars={"FEAT_KEY": "feat-val"},
        ).compute("alpha", resolve_commands=False)
        assert result["WS_KEY"] == "ws-val"
        assert result["FEAT_KEY"] == "feat-val"

    def test_named_band_wins_over_feature_band(self) -> None:
        """A per-env override replaces the feature-band value for that key."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"BZ_HUB_URL": "http://127.0.0.1:${WINTER_PORT_BASE+2}"},
            named_vars={"alpha": {"BZ_HUB_URL": "http://127.0.0.1:8421"}},
        ).compute("alpha", resolve_commands=False)
        assert result["BZ_HUB_URL"] == "http://127.0.0.1:8421"

    def test_named_band_does_not_leak_to_sibling_env(self) -> None:
        """A sibling env with no band of its own still gets the feature-band value."""
        result = _svc(
            assignments={"alpha": 1, "beta": 2},
            feature_vars={"BZ_HUB_URL": "http://127.0.0.1:${WINTER_PORT_BASE+2}"},
            named_vars={"alpha": {"BZ_HUB_URL": "http://127.0.0.1:8421"}},
        ).compute("beta", resolve_commands=False)
        # beta's index is 2 → port base 4040 → +2 = 4042.
        assert result["BZ_HUB_URL"] == "http://127.0.0.1:4042"

    def test_named_band_for_unknown_env_is_inert(self) -> None:
        """A band naming an env that does not exist never fires — no error, no key."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"FEAT": "feat-value"},
            named_vars={"nonexistent": {"FEAT": "override", "ONLY_THERE": "x"}},
        ).compute("alpha", resolve_commands=False)
        assert result["FEAT"] == "feat-value"
        assert "ONLY_THERE" not in result

    def test_named_band_adds_env_only_key(self) -> None:
        """An override may introduce a key no other band declares."""
        result = _svc(
            assignments={"alpha": 1},
            named_vars={"alpha": {"ALPHA_ONLY": "just-alpha"}},
        ).compute("alpha", resolve_commands=False)
        assert result["ALPHA_ONLY"] == "just-alpha"

    def test_named_band_renders_alone_when_other_bands_empty(self) -> None:
        """The override band is rendered even when workspace and feature bands are empty."""
        result = _svc(
            assignments={"alpha": 1},
            named_vars={"alpha": {"SOLO": "value"}},
        ).compute("alpha", resolve_commands=False)
        assert result["SOLO"] == "value"

    def test_named_band_template_resolves_feature_port_base(self) -> None:
        """An override template has WINTER_PORT_BASE in scope, like the feature band."""
        result = _svc(
            assignments={"alpha": 1},
            named_vars={"alpha": {"PORT": "${WINTER_PORT_BASE+5}"}},
        ).compute("alpha", resolve_commands=False)
        assert result["PORT"] == "4025"

    def test_named_band_template_references_lower_band_key(self) -> None:
        """An override template may reference a workspace- or feature-band key."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"DB_HOST": "db.example.com"},
            feature_vars={"DB_NAME": "featdb"},
            named_vars={"alpha": {"DB_URL": "postgres://${DB_HOST}/${DB_NAME}"}},
        ).compute("alpha", resolve_commands=False)
        assert result["DB_URL"] == "postgres://db.example.com/featdb"

    def test_named_band_wins_over_workspace_band(self) -> None:
        """An override beats the workspace band too, not just the feature band."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"COMMON": "from-workspace"},
            named_vars={"alpha": {"COMMON": "from-alpha"}},
        ).compute("alpha", resolve_commands=False)
        assert result["COMMON"] == "from-alpha"

    def test_workspace_scope_ignores_named_bands(self) -> None:
        """Per-env override bands never render for the workspace scope."""
        result = _svc(
            workspace_vars={"COMMON": "from-workspace"},
            named_vars={"workspace": {"COMMON": "should-not-apply"}},
        ).compute("workspace", resolve_commands=False)
        assert result["COMMON"] == "from-workspace"

    def test_feature_scope_empty_bands_returns_base_vars_only(self) -> None:
        """Absent bands for feature scope: only the five WINTER_* base vars."""
        result = _svc(assignments={"alpha": 1}, feature_vars=None).compute("alpha", resolve_commands=False)
        assert set(result.keys()) == {
            "WINTER_ENV",
            "WINTER_ENV_INDEX",
            "WINTER_PORT_BASE",
            "WINTER_WORKSPACE_PORT_BASE",
            "WINTER_SERVICE_PREFIX",
        }


# ---------------------------------------------------------------------------
# Band rendering — existing token tests (migrated to feature_vars=)
# ---------------------------------------------------------------------------


class TestEnvVarsRendering:
    def test_port_offset_token(self) -> None:
        """${WINTER_PORT_BASE+10} resolves to port_base + 10."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"WEB_PORT": "${WINTER_PORT_BASE+10}"},
        ).compute("alpha", resolve_commands=False)
        assert result["WEB_PORT"] == "4030"  # 4020 + 10

    def test_zero_offset(self) -> None:
        """${WINTER_PORT_BASE+0} resolves to exactly port_base."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"MY_PORT": "${WINTER_PORT_BASE+0}"},
        ).compute("alpha", resolve_commands=False)
        assert result["MY_PORT"] == "4020"

    def test_literal_passthrough(self) -> None:
        """Values with no ${...} token pass through unchanged."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"DATABASE_URL": "postgresql://user:pass@localhost/mydb"},
        ).compute("alpha", resolve_commands=False)
        assert result["DATABASE_URL"] == "postgresql://user:pass@localhost/mydb"

    def test_bare_reference_resolves(self) -> None:
        """${WINTER_PORT_BASE} without offset resolves to the base var's string value."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"MY_PORT": "${WINTER_PORT_BASE}"},
        ).compute("alpha", resolve_commands=False)
        assert result["MY_PORT"] == "4020"

    def test_sibling_reference_resolves(self) -> None:
        """A later [env.feature.vars] entry can reference an earlier one by name."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={
                "DB_PORT": "${WINTER_PORT_BASE+12}",
                "DATABASE_URL": "postgresql://localhost:${DB_PORT}/mydb",
            },
        ).compute("alpha", resolve_commands=False)
        assert result["DB_PORT"] == "4032"
        assert result["DATABASE_URL"] == "postgresql://localhost:4032/mydb"

    def test_workspace_port_base_arithmetic(self) -> None:
        """${WINTER_WORKSPACE_PORT_BASE+N} resolves against index-0 base."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"RABBITMQ_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        ).compute("alpha", resolve_commands=False)
        assert result["RABBITMQ_PORT"] == "4001"

    def test_string_base_var_reference(self) -> None:
        """${WINTER_ENV} resolves to the env name string."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"TAG": "${WINTER_ENV}-build"},
        ).compute("alpha", resolve_commands=False)
        assert result["TAG"] == "alpha-build"

    def test_mixed_token_and_literal(self) -> None:
        """A value mixing token with surrounding text resolves correctly."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"DB_URL": "postgres://localhost:${WINTER_PORT_BASE+12}/db"},
        ).compute("alpha", resolve_commands=False)
        assert result["DB_URL"] == "postgres://localhost:4032/db"

    def test_multiple_port_offsets(self) -> None:
        """Multiple [env.feature.vars] entries are all rendered."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={
                "WEB_PORT": "${WINTER_PORT_BASE+10}",
                "API_PORT": "${WINTER_PORT_BASE+11}",
                "LITERAL": "no-token",
            },
        ).compute("alpha", resolve_commands=False)
        assert result["WEB_PORT"] == "4030"
        assert result["API_PORT"] == "4031"
        assert result["LITERAL"] == "no-token"

    def test_no_env_vars_table_returns_base_vars_only(self) -> None:
        """Absent bands return only the five base WINTER_* vars."""
        result = _svc(assignments={"alpha": 1}, feature_vars=None).compute("alpha", resolve_commands=False)
        assert set(result.keys()) == {
            "WINTER_ENV",
            "WINTER_ENV_INDEX",
            "WINTER_PORT_BASE",
            "WINTER_WORKSPACE_PORT_BASE",
            "WINTER_SERVICE_PREFIX",
        }

    def test_workspace_scope_env_vars(self) -> None:
        """[env.workspace.vars] entries are rendered for workspace scope."""
        result = _svc(
            workspace_vars={"WS_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        ).compute("workspace", resolve_commands=False)
        assert result["WS_PORT"] == "4001"  # 4000 + 1


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestEnvVarsErrors:
    def test_undefined_reference_raises(self) -> None:
        """A ${NAME} reference to an undefined variable raises ValueError."""
        with pytest.raises(ValueError, match=r"undefined variable.*UNKNOWN_VAR"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={"BAD": "${UNKNOWN_VAR}"},
            ).compute("alpha", resolve_commands=False)

    def test_unsupported_token_raises(self) -> None:
        """A ${...} that is not a valid reference pattern raises ValueError."""
        with pytest.raises(ValueError, match="unsupported substitution token"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={"BAD": "${not-an-identifier}"},
            ).compute("alpha", resolve_commands=False)

    def test_non_integer_offset_raises(self) -> None:
        """${NAME+N} where NAME is not an integer raises ValueError."""
        with pytest.raises(ValueError, match="non-integer"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={
                    "HOSTNAME": "db.example.com",
                    "BAD": "${HOSTNAME+1}",
                },
            ).compute("alpha", resolve_commands=False)

    def test_forward_reference_resolves_under_the_fixpoint(self) -> None:
        """Referencing an entry declared later in the same band now resolves.

        Single-pass rendering used to raise immediately on a forward reference;
        the fixpoint drops declaration-order as a constraint entirely (see
        ``EnvBandResolverService``), so this now succeeds identically to the
        sibling-reference case with the declarations reversed.
        """
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={
                "DATABASE_URL": "postgres://localhost:${WTS_DB_PORT}/db",
                "WTS_DB_PORT": "${WINTER_PORT_BASE+12}",
            },
        ).compute("alpha", resolve_commands=False)
        assert result["WTS_DB_PORT"] == "4032"
        assert result["DATABASE_URL"] == "postgres://localhost:4032/db"

    def test_command_entry_resolves_to_placeholder_when_gated(self) -> None:
        """resolve_commands=False masks a command entry to the placeholder without running it."""
        runner = FakeCommandEntryRunner()
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"SECRET": EnvCommandEntry(command="echo hi")},
            runner=runner,
        ).compute("alpha", resolve_commands=False)
        assert result["SECRET"] == COMMAND_PLACEHOLDER
        assert runner.calls == []

    def test_command_entry_runs_when_resolve_commands_is_true(self) -> None:
        """resolve_commands=True is forwarded through compute() to the resolver, which runs the command."""
        runner = FakeCommandEntryRunner({"echo hi": "hunter2"})
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"SECRET": EnvCommandEntry(command="echo hi")},
            runner=runner,
        ).compute("alpha", resolve_commands=True)
        assert result["SECRET"] == "hunter2"
        assert len(runner.calls) == 1
