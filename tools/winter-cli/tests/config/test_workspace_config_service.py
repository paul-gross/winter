from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.config.models import _DEFAULT_ENV_ALIASES, AdoptExtensions, DashboardLayout, SingletonType
from winter_cli.config.workspace import (
    CONFIG_FILE,
    LOCAL_CONFIG_FILE,
    WINTER_DIR,
    WorkspaceConfigService,
)
from winter_cli.core.config_file import ConfigError

WORKSPACE_ROOT = Path("/ws/demo")


class _StubLocator:
    """IWorkspaceLocator fake — returns a fixed path instead of walking cwd."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def find_workspace_root(self) -> Path:
        return self._root


class _DictConfigFileReader:
    """IConfigFileReader fake — returns canned dicts keyed by path."""

    def __init__(self, contents: dict[Path, dict]) -> None:
        self._contents = contents

    def load(self, path: Path) -> dict:
        if path not in self._contents:
            raise FileNotFoundError(path)
        return self._contents[path]


def _service(
    fs: FakeFilesystem,
    configs: dict[Path, dict],
    root: Path = WORKSPACE_ROOT,
) -> WorkspaceConfigService:
    return WorkspaceConfigService(
        workspace_locator=_StubLocator(root),
        fs=fs,
        config_file_reader=_DictConfigFileReader(configs),
    )


def test_load_reads_shared_config() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})  # presence-only; reader returns canned dict
    svc = _service(
        fs,
        {
            config_path: {
                "main_branch": "trunk",
                "session_prefix": "ws",
                "git_excludes": ["/.idea/"],
                "git": {"user": {"name": "Test User", "email": "test@example.com"}},
                "project_repository": [
                    {"name": "frontend", "url": "git@example.com:org/frontend.git", "pinned": True},
                ],
                "standalone_repository": [
                    {"name": "ext-one", "url": "git@example.com:org/ext-one.git"},
                ],
            },
        },
    )

    config = svc.load()

    assert config.workspace_root == WORKSPACE_ROOT
    assert config.session_prefix == "ws"
    assert config.main_branch == "trunk"
    assert config.git_excludes == ["/.idea/"]
    assert config.git_identity is not None
    assert config.git_identity.name == "Test User"
    assert config.git_identity.email == "test@example.com"
    assert config.adopt_extensions == AdoptExtensions.winter

    assert any(r.type == SingletonType.workspace for r in config.singleton_repos)
    assert len(config.project_repos) == 1
    assert config.project_repos[0].name == "frontend"
    assert config.project_repos[0].pinned is True
    assert len(config.standalone_repos) == 1
    assert config.standalone_repos[0].name == "ext-one"


def test_load_maps_workspace_doctor_and_lint_scripts() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"doctor": "ai/doctor.sh", "lint": "ai/lint.sh"}})

    config = svc.load()

    assert config.doctor == "ai/doctor.sh"
    assert config.lint == ["ai/lint.sh"]


def test_load_maps_service_orchestrator() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"service_orchestrator": "winter-service-tmux"}})

    assert svc.load().service_orchestrator == "winter-service-tmux"


def test_load_service_orchestrator_defaults_to_none() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    assert svc.load().service_orchestrator is None


def test_load_accepts_lint_as_a_list() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"lint": ["ai/a.sh", "ai/b.sh"]}})

    config = svc.load()

    assert config.lint == ["ai/a.sh", "ai/b.sh"]


def test_load_merges_local_overlay() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "main_branch": "trunk",
                "project_repository": [
                    {"name": "frontend", "url": "git@example.com:org/frontend.git"},
                ],
            },
            local_path: {
                "main_branch": "develop",
                "project_repository": [
                    {"name": "backend", "url": "git@example.com:org/backend.git"},
                ],
            },
        },
    )

    config = svc.load()

    # Scalars in the overlay win.
    assert config.main_branch == "develop"
    # Arrays of tables concatenate via deep_merge.
    names = sorted(r.name for r in config.project_repos if r.name)
    assert names == ["backend", "frontend"]


def test_load_picks_up_singletons_present_on_disk() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(
        files={
            config_path: "",
            WORKSPACE_ROOT / "ai" / "harness" / ".git": "",  # treated as exists()
        },
        directories=[WORKSPACE_ROOT / "product"],
    )
    svc = _service(fs, {config_path: {}})

    config = svc.load()

    types = {r.type for r in config.singleton_repos}
    assert SingletonType.product in types
    assert SingletonType.harness in types


def test_load_rejects_invalid_adopt_extensions() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"adopt_extensions": "bogus"}})

    with pytest.raises(ConfigError, match="adopt_extensions"):
        svc.load()


def test_load_returns_empty_when_config_files_absent() -> None:
    """No config files present → load() succeeds with defaults (empty overlay path)."""
    fs = FakeFilesystem()  # nothing seeded
    svc = _service(fs, {})  # config_file_reader would be called only if is_file() said yes

    config = svc.load()
    assert config.workspace_root == WORKSPACE_ROOT
    assert config.main_branch == "main"  # default
    assert config.project_repos == []


def test_keybindings_default_when_absent() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    kb = svc.load().keybindings
    assert kb.leader == "\\"
    assert kb.timeoutlen == 1000
    assert kb.bindings == {}


def test_keybindings_parsed_from_table() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "keybindings": {
                    "leader": ",",
                    "timeoutlen": 400,
                    "bindings": {
                        "workspace.refresh": "g",
                        "worktree.open_detail": "o",
                    },
                },
            },
        },
    )

    kb = svc.load().keybindings
    assert kb.leader == ","
    assert kb.timeoutlen == 400
    assert kb.bindings == {"workspace.refresh": "g", "worktree.open_detail": "o"}


def test_keybindings_overlay_overrides_per_key() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {"keybindings": {"timeoutlen": 1000, "bindings": {"workspace.refresh": "g"}}},
            local_path: {"keybindings": {"bindings": {"workspace.refresh": "R"}}},
        },
    )

    kb = svc.load().keybindings
    # Per-machine overlay wins for the overridden id.
    assert kb.bindings["workspace.refresh"] == "R"
    assert kb.timeoutlen == 1000


def test_capabilities_parsed_from_table_string() -> None:
    """capabilities.service = "tmux" (string) is normalized to a one-element list."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"capabilities": {"service": "winter-service-tmux"}}})

    assert svc.load().capabilities == {"service": ["winter-service-tmux"]}


def test_capabilities_parsed_from_table_list() -> None:
    """capabilities.service = [...] (list) is stored as an ordered list."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {config_path: {"capabilities": {"service": ["winter-service-docker", "winter-service-tmux"]}}},
    )

    assert svc.load().capabilities == {"service": ["winter-service-docker", "winter-service-tmux"]}


def test_capabilities_list_deduplicates_preserving_order() -> None:
    """Duplicate entries in capabilities.service list are removed preserving first occurrence."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"capabilities": {"service": ["tmux", "docker", "tmux"]}}})

    assert svc.load().capabilities == {"service": ["tmux", "docker"]}


def test_capabilities_overlay_overrides_per_key() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {"capabilities": {"service": "winter-service-tmux"}},
            local_path: {"capabilities": {"service": "my-local-orchestrator"}},
        },
    )

    # Local overlay wins for the overridden slot.
    assert svc.load().capabilities == {"service": ["my-local-orchestrator"]}


def test_capabilities_aliased_from_service_orchestrator() -> None:
    """Legacy service_orchestrator key folds into capabilities["service"] as a one-element list."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"service_orchestrator": "winter-service-tmux"}})

    config = svc.load()
    assert config.capabilities == {"service": ["winter-service-tmux"]}


def test_capabilities_explicit_wins_over_service_orchestrator_alias() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "capabilities": {"service": "A"},
                "service_orchestrator": "B",
            }
        },
    )

    config = svc.load()
    assert config.capabilities["service"] == ["A"]


def test_capabilities_empty_when_neither_key_present() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    assert svc.load().capabilities == {}


# ── port allocation config knobs ─────────────────────────────────────────────


def test_port_config_defaults() -> None:
    """Omitting port knobs from config.toml produces the documented defaults."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    config = svc.load()

    assert config.base_port == 4000
    assert config.ports_per_env == 20
    assert config.env_aliases == list(_DEFAULT_ENV_ALIASES)
    assert config.envs_per_workspace == 48


def test_port_config_knobs_parsed_from_config() -> None:
    """Explicit values in config.toml override the defaults."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "base_port": 5000,
                "ports_per_env": 50,
                "env_aliases": ["dev", "staging"],
                "envs_per_workspace": 10,
            }
        },
    )

    config = svc.load()

    assert config.base_port == 5000
    assert config.ports_per_env == 50
    assert config.env_aliases == ["dev", "staging"]
    assert config.envs_per_workspace == 10


def test_port_config_local_overlay_overrides_scalar_knobs() -> None:
    """config.local.toml scalar values override config.toml values for base_port, ports_per_env, envs_per_workspace."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "base_port": 4000,
                "ports_per_env": 20,
                "env_aliases": [],
                "envs_per_workspace": 48,
            },
            local_path: {
                "base_port": 6000,
                "ports_per_env": 30,
                "envs_per_workspace": 20,
            },
        },
    )

    config = svc.load()

    # Scalars in the overlay win.
    assert config.base_port == 6000
    assert config.ports_per_env == 30
    assert config.envs_per_workspace == 20


def test_port_config_local_overlay_replaces_env_aliases_list() -> None:
    """config.local.toml env_aliases scalar list replaces the base list (not appended)."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "env_aliases": ["alpha", "beta", "gamma", "delta", "epsilon"],
                "envs_per_workspace": 48,
            },
            local_path: {
                # Overlay trims env_aliases to just two entries.
                "env_aliases": ["alpha", "beta"],
            },
        },
    )

    config = svc.load()

    # Scalar lists are replaced by the overlay, not appended to.
    assert config.env_aliases == ["alpha", "beta"]


def test_envs_per_workspace_validation_rejects_too_small() -> None:
    """Config load raises RuntimeError when envs_per_workspace < len(env_aliases) + 2."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    # 3 aliases requires envs_per_workspace >= 5; providing 4 is too small.
    svc = _service(
        fs,
        {
            config_path: {
                "env_aliases": ["a", "b", "c"],
                "envs_per_workspace": 4,
            }
        },
    )

    with pytest.raises(ConfigError, match="envs_per_workspace"):
        svc.load()


def test_envs_per_workspace_validation_accepts_exact_minimum() -> None:
    """envs_per_workspace == len(env_aliases) + 2 is exactly valid."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    # 3 aliases → minimum envs_per_workspace = 5.
    svc = _service(
        fs,
        {
            config_path: {
                "env_aliases": ["a", "b", "c"],
                "envs_per_workspace": 5,
            }
        },
    )

    config = svc.load()
    assert config.envs_per_workspace == 5
    assert config.env_aliases == ["a", "b", "c"]


def test_empty_env_aliases_is_valid_with_default_envs_per_workspace() -> None:
    """Empty env_aliases with default envs_per_workspace=48 is valid (48 >= 0 + 2)."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"env_aliases": []}})

    config = svc.load()
    assert config.env_aliases == []
    assert config.envs_per_workspace == 48


def test_port_base_for_index_uses_config_values() -> None:
    """config.port_base_for_index(index) = base_port + index * ports_per_env."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    # Empty env_aliases so envs_per_workspace=10 passes the >= 0+2 invariant.
    svc = _service(
        fs,
        {config_path: {"base_port": 5000, "ports_per_env": 50, "env_aliases": [], "envs_per_workspace": 10}},
    )

    config = svc.load()

    # index=0 → base
    assert config.port_base_for_index(0) == 5000
    # index=1 → 5000 + 1 * 50 = 5050
    assert config.port_base_for_index(1) == 5050
    # index=3 → 5000 + 3 * 50 = 5150
    assert config.port_base_for_index(3) == 5150


def test_port_base_for_index_default_config_beta() -> None:
    """With default config, beta (index=2) → 4000 + 2*20 = 4040."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    config = svc.load()

    # Default: base_port=4000, ports_per_env=20
    # beta has index 2 in the default env_aliases list
    assert config.port_base_for_index(2) == 4000 + 2 * 20


# ── dashboard layout config ───────────────────────────────────────────────────


def test_dashboard_layout_default_when_absent() -> None:
    """No [tui.dashboard] table → dashboard.layout defaults to DashboardLayout.auto."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {}})

    assert svc.load().dashboard.layout == DashboardLayout.auto


def test_dashboard_layout_explicit_auto() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"tui": {"dashboard": {"layout": "auto"}}}})

    assert svc.load().dashboard.layout == DashboardLayout.auto


def test_dashboard_layout_repos_as_columns() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"tui": {"dashboard": {"layout": "repos-as-columns"}}}})

    assert svc.load().dashboard.layout == DashboardLayout.repos_as_columns


def test_dashboard_layout_repos_as_rows() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"tui": {"dashboard": {"layout": "repos-as-rows"}}}})

    assert svc.load().dashboard.layout == DashboardLayout.repos_as_rows


def test_dashboard_layout_list() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"tui": {"dashboard": {"layout": "list"}}}})

    assert svc.load().dashboard.layout == DashboardLayout.list


def test_dashboard_layout_invalid_raises_config_error() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"tui": {"dashboard": {"layout": "grid"}}}})

    with pytest.raises(ConfigError, match=r"tui\.dashboard\.layout"):
        svc.load()


def test_dashboard_layout_overlay_overrides_base() -> None:
    """config.local.toml overlay overrides [tui.dashboard] from the base config."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE
    fs = FakeFilesystem(files={config_path: "", local_path: ""})
    svc = _service(
        fs,
        {
            config_path: {"tui": {"dashboard": {"layout": "repos-as-rows"}}},
            local_path: {"tui": {"dashboard": {"layout": "list"}}},
        },
    )

    assert svc.load().dashboard.layout == DashboardLayout.list


# ── capabilities.<slot> = str | list[str] (R2) ────────────────────────────────


def test_capabilities_service_string_folds_to_single_element_list() -> None:
    """capabilities.service = "tmux" (bare string) → ["tmux"] internally."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"capabilities": {"service": "winter-service-tmux"}}})

    config = svc.load()
    assert config.capabilities["service"] == ["winter-service-tmux"]


def test_service_orchestrator_single_string_normalizes_to_one_element_capabilities_list() -> None:
    """Legacy service_orchestrator (singular) folds into capabilities["service"] as a one-element list."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(fs, {config_path: {"service_orchestrator": "winter-service-tmux"}})

    config = svc.load()
    assert config.capabilities["service"] == ["winter-service-tmux"]


def test_capabilities_service_list_stored_in_declared_order() -> None:
    """capabilities.service = ["tmux", "docker"] → list stored in declared order."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {config_path: {"capabilities": {"service": ["winter-service-tmux", "winter-service-docker"]}}},
    )

    config = svc.load()
    assert config.capabilities["service"] == ["winter-service-tmux", "winter-service-docker"]


def test_capabilities_service_list_explicit_wins_over_legacy_key() -> None:
    """When capabilities.service is set, the legacy service_orchestrator key is ignored."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    svc = _service(
        fs,
        {
            config_path: {
                "capabilities": {"service": ["A", "B"]},
                "service_orchestrator": "ignored",
            }
        },
    )

    config = svc.load()
    assert config.capabilities["service"] == ["A", "B"]
