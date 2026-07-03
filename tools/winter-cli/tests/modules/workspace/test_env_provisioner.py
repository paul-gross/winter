"""Tests for EnvProvisionerService — the single source of truth for runtime env maps.

Covers:
- Feature-env scope: WINTER_ENV / WINTER_ENV_INDEX / WINTER_PORT_BASE /
  WINTER_WORKSPACE_PORT_BASE are computed from the registry-assigned index.
- Workspace scope: index 0, port_base_for_index(0).
- Band selection: workspace scope → workspace band only; feature scope → union
  with feature winning collisions; workspace keys visible to feature templates.
- Env-band rendering: ${NAME}, ${NAME+N} expansion, sibling references.
- Env-band error cases: undefined variable, unsupported token, non-integer +N.
- C4 invariant: workspace-band entries resolve identically at workspace and feature scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winter_cli.config.models import (
    EnvVarBands,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    WorkspaceConfig,
)
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
    workspace_vars: dict[str, str] | None = None,
    feature_vars: dict[str, str] | None = None,
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
    if workspace_vars is not None or feature_vars is not None:
        kwargs["env_bands"] = EnvVarBands(
            workspace=workspace_vars or {},
            feature=feature_vars or {},
        )
    if service_prefix is not None:
        kwargs["service_prefix"] = service_prefix
    return WorkspaceConfig(**kwargs)


def _svc(
    assignments: dict[str, int] | None = None,
    base_port: int = 4000,
    ports_per_env: int = 20,
    workspace_vars: dict[str, str] | None = None,
    feature_vars: dict[str, str] | None = None,
    service_prefix: str | None = None,
) -> EnvProvisionerService:
    cfg = _config(
        base_port=base_port,
        ports_per_env=ports_per_env,
        workspace_vars=workspace_vars,
        feature_vars=feature_vars,
        service_prefix=service_prefix,
    )
    reg = _InMemoryRegistry(assignments)
    return EnvProvisionerService(config=cfg, registry=reg)


# ---------------------------------------------------------------------------
# Feature-env scope
# ---------------------------------------------------------------------------


class TestFeatureEnvScope:
    def test_winter_env_is_scope_name(self) -> None:
        """WINTER_ENV equals the scope name passed to compute()."""
        result = _svc(assignments={"alpha": 1}).compute("alpha")
        assert result["WINTER_ENV"] == "alpha"

    def test_winter_env_index_from_registry(self) -> None:
        """WINTER_ENV_INDEX matches the registry-assigned index."""
        result = _svc(assignments={"alpha": 1}).compute("alpha")
        assert result["WINTER_ENV_INDEX"] == "1"

    def test_winter_port_base_alpha(self) -> None:
        """WINTER_PORT_BASE is base_port + index * ports_per_env for alpha (index 1)."""
        result = _svc(assignments={"alpha": 1}, base_port=4000, ports_per_env=20).compute("alpha")
        assert result["WINTER_PORT_BASE"] == "4020"  # 4000 + 1 * 20

    def test_winter_port_base_beta(self) -> None:
        """WINTER_PORT_BASE is correct for beta (index 2)."""
        result = _svc(assignments={"beta": 2}, base_port=4000, ports_per_env=20).compute("beta")
        assert result["WINTER_PORT_BASE"] == "4040"  # 4000 + 2 * 20

    def test_winter_workspace_port_base_is_index_zero(self) -> None:
        """WINTER_WORKSPACE_PORT_BASE is always port_base_for_index(0) = base_port."""
        result = _svc(assignments={"alpha": 1}, base_port=4000, ports_per_env=20).compute("alpha")
        assert result["WINTER_WORKSPACE_PORT_BASE"] == "4000"

    def test_winter_service_prefix_matches_config(self) -> None:
        """WINTER_SERVICE_PREFIX equals the resolved config.service_prefix for a feature scope."""
        result = _svc(assignments={"alpha": 1}, service_prefix="myproj").compute("alpha")
        assert result["WINTER_SERVICE_PREFIX"] == "myproj"

    def test_persisted_index_used_over_formula(self) -> None:
        """A non-alias env with a persisted index uses that index, not the hash formula."""
        # "myenv" is not in env_aliases; persist index 15 out-of-band.
        result = _svc(assignments={"myenv": 15}, base_port=4000, ports_per_env=20).compute("myenv")
        assert result["WINTER_ENV_INDEX"] == "15"
        assert result["WINTER_PORT_BASE"] == "4300"  # 4000 + 15 * 20


# ---------------------------------------------------------------------------
# Workspace scope
# ---------------------------------------------------------------------------


class TestWorkspaceScope:
    def test_winter_env_is_workspace(self) -> None:
        result = _svc().compute("workspace")
        assert result["WINTER_ENV"] == "workspace"

    def test_winter_env_index_is_zero(self) -> None:
        result = _svc().compute("workspace")
        assert result["WINTER_ENV_INDEX"] == "0"

    def test_winter_workspace_port_base_is_index_zero(self) -> None:
        """WINTER_WORKSPACE_PORT_BASE is base_port for workspace scope (index 0)."""
        result = _svc(base_port=4000, ports_per_env=20).compute("workspace")
        assert result["WINTER_WORKSPACE_PORT_BASE"] == "4000"

    def test_winter_port_base_not_emitted_for_workspace(self) -> None:
        """WINTER_PORT_BASE is NOT in the workspace scope result — workspace only gets WINTER_WORKSPACE_PORT_BASE."""
        result = _svc(base_port=4000, ports_per_env=20).compute("workspace")
        assert "WINTER_PORT_BASE" not in result

    def test_winter_service_prefix_matches_config(self) -> None:
        """WINTER_SERVICE_PREFIX equals the resolved config.service_prefix for the workspace scope."""
        result = _svc(service_prefix="myproj").compute("workspace")
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
        ).compute("workspace")
        assert result["SHARED"] == "ws-only"
        assert "FEAT" not in result

    def test_workspace_scope_omits_winter_port_base(self) -> None:
        """Workspace scope still does not emit WINTER_PORT_BASE even with workspace vars."""
        result = _svc(
            workspace_vars={"WS_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        ).compute("workspace")
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
            ).compute("workspace")

    def test_workspace_scope_empty_bands_returns_base_vars_only(self) -> None:
        """Absent bands for workspace scope: only the four WINTER_* base vars."""
        result = _svc().compute("workspace")
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
        ws_result = svc.compute("workspace")
        feat_result = svc.compute("alpha")
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
            ).compute("alpha")

    def test_feature_band_winter_port_base_resolves_to_feature_base(self) -> None:
        """${WINTER_PORT_BASE+N} in the feature band still resolves to the feature's port base."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"APP_PORT": "${WINTER_PORT_BASE+5}"},
        ).compute("alpha")
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
        ).compute("alpha")
        assert result["SHARED"] == "ws-value"

    def test_feature_scope_includes_feature_band_key(self) -> None:
        """Feature scope includes feature-band entries."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"FEAT": "feat-value"},
        ).compute("alpha")
        assert result["FEAT"] == "feat-value"

    def test_feature_wins_on_key_collision(self) -> None:
        """When the same key appears in both bands, the feature value wins."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"COMMON": "from-workspace"},
            feature_vars={"COMMON": "from-feature"},
        ).compute("alpha")
        assert result["COMMON"] == "from-feature"

    def test_workspace_scope_gets_workspace_value_on_same_key(self) -> None:
        """Workspace scope still sees the workspace value for the colliding key."""
        result = _svc(
            workspace_vars={"COMMON": "from-workspace"},
            feature_vars={"COMMON": "from-feature"},
        ).compute("workspace")
        assert result["COMMON"] == "from-workspace"

    def test_feature_template_references_workspace_band_key(self) -> None:
        """A feature-band template can reference a workspace-band key already rendered."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"DB_HOST": "db.example.com"},
            feature_vars={"DB_URL": "postgres://${DB_HOST}/mydb"},
        ).compute("alpha")
        assert result["DB_HOST"] == "db.example.com"
        assert result["DB_URL"] == "postgres://db.example.com/mydb"

    def test_feature_scope_union_both_bands(self) -> None:
        """Feature scope output contains keys from both bands."""
        result = _svc(
            assignments={"alpha": 1},
            workspace_vars={"WS_KEY": "ws-val"},
            feature_vars={"FEAT_KEY": "feat-val"},
        ).compute("alpha")
        assert result["WS_KEY"] == "ws-val"
        assert result["FEAT_KEY"] == "feat-val"

    def test_feature_scope_empty_bands_returns_base_vars_only(self) -> None:
        """Absent bands for feature scope: only the five WINTER_* base vars."""
        result = _svc(assignments={"alpha": 1}, feature_vars=None).compute("alpha")
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
        ).compute("alpha")
        assert result["WEB_PORT"] == "4030"  # 4020 + 10

    def test_zero_offset(self) -> None:
        """${WINTER_PORT_BASE+0} resolves to exactly port_base."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"MY_PORT": "${WINTER_PORT_BASE+0}"},
        ).compute("alpha")
        assert result["MY_PORT"] == "4020"

    def test_literal_passthrough(self) -> None:
        """Values with no ${...} token pass through unchanged."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"DATABASE_URL": "postgresql://user:pass@localhost/mydb"},
        ).compute("alpha")
        assert result["DATABASE_URL"] == "postgresql://user:pass@localhost/mydb"

    def test_bare_reference_resolves(self) -> None:
        """${WINTER_PORT_BASE} without offset resolves to the base var's string value."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"MY_PORT": "${WINTER_PORT_BASE}"},
        ).compute("alpha")
        assert result["MY_PORT"] == "4020"

    def test_sibling_reference_resolves(self) -> None:
        """A later [env.feature.vars] entry can reference an earlier one by name."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={
                "DB_PORT": "${WINTER_PORT_BASE+12}",
                "DATABASE_URL": "postgresql://localhost:${DB_PORT}/mydb",
            },
        ).compute("alpha")
        assert result["DB_PORT"] == "4032"
        assert result["DATABASE_URL"] == "postgresql://localhost:4032/mydb"

    def test_workspace_port_base_arithmetic(self) -> None:
        """${WINTER_WORKSPACE_PORT_BASE+N} resolves against index-0 base."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"RABBITMQ_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
        ).compute("alpha")
        assert result["RABBITMQ_PORT"] == "4001"

    def test_string_base_var_reference(self) -> None:
        """${WINTER_ENV} resolves to the env name string."""
        result = _svc(
            assignments={"alpha": 1},
            feature_vars={"TAG": "${WINTER_ENV}-build"},
        ).compute("alpha")
        assert result["TAG"] == "alpha-build"

    def test_mixed_token_and_literal(self) -> None:
        """A value mixing token with surrounding text resolves correctly."""
        result = _svc(
            assignments={"alpha": 1},
            base_port=4000,
            ports_per_env=20,
            feature_vars={"DB_URL": "postgres://localhost:${WINTER_PORT_BASE+12}/db"},
        ).compute("alpha")
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
        ).compute("alpha")
        assert result["WEB_PORT"] == "4030"
        assert result["API_PORT"] == "4031"
        assert result["LITERAL"] == "no-token"

    def test_no_env_vars_table_returns_base_vars_only(self) -> None:
        """Absent bands return only the five base WINTER_* vars."""
        result = _svc(assignments={"alpha": 1}, feature_vars=None).compute("alpha")
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
        ).compute("workspace")
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
            ).compute("alpha")

    def test_unsupported_token_raises(self) -> None:
        """A ${...} that is not a valid reference pattern raises ValueError."""
        with pytest.raises(ValueError, match="unsupported substitution token"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={"BAD": "${not-an-identifier}"},
            ).compute("alpha")

    def test_non_integer_offset_raises(self) -> None:
        """${NAME+N} where NAME is not an integer raises ValueError."""
        with pytest.raises(ValueError, match="non-integer"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={
                    "HOSTNAME": "db.example.com",
                    "BAD": "${HOSTNAME+1}",
                },
            ).compute("alpha")

    def test_forward_reference_raises(self) -> None:
        """Referencing an entry declared later (not yet in scope) raises ValueError."""
        with pytest.raises(ValueError, match=r"undefined variable.*WTS_DB_PORT"):
            _svc(
                assignments={"alpha": 1},
                feature_vars={
                    "DATABASE_URL": "postgres://localhost:${WTS_DB_PORT}/db",
                    "WTS_DB_PORT": "${WINTER_PORT_BASE+12}",
                },
            ).compute("alpha")
