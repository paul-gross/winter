"""Phase 3 integration tests: registry wired into env lifecycle and read path.

Tests prove:
  (a) winter ws init records the allocated index to state.toml and the
      resulting .winter.env WINTER_ENV_INDEX/WINTER_PORT_BASE match the
      registry-assigned index + port_base_for_index.
  (b) winter ws destroy removes the registry entry (and does NOT remove it
      on --dry-run).
  (c) ReadWorkspaceRepository._build_environment returns the registry-recorded
      index when present, and falls back to resolve_env_index when absent
      (pre-registry env).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeFilesystem,
    FakeGitRepository,
    FakeInitReporter,
    FakeSubprocessRunner,
)
from winter_cli.config.models import (
    AdoptExtensions,
    GitIdentity,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.core.internal.local_filesystem import LocalFilesystem
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.env_index import resolve_env_index
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry
from winter_cli.modules.workspace.agents_md_service import AgentsMdService
from winter_cli.modules.workspace.extension_claudemd_service import ExtensionClaudemdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.read_workspace_repository import ReadWorkspaceRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.toml_env_index_registry import TomlEnvIndexRegistry
from winter_cli.modules.workspace.models import ProjectRepository, Workspace
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")


# ---------------------------------------------------------------------------
# In-memory registry for service-level tests (avoids real I/O)
# ---------------------------------------------------------------------------


class _InMemoryRegistry:
    """Minimal in-memory IEnvIndexRegistry for tests that don't need persistence."""

    def __init__(self) -> None:
        self._data: dict[str, int] = {}

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _default_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
    )


def _init_service(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    subprocess: FakeSubprocessRunner,
    git: FakeGitRepository,
    registry: IEnvIndexRegistry,
) -> InitService:
    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({}))
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config),
        extension_symlink_svc=ExtensionSymlinkService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        extension_hook_svc=ExtensionHookService(
            config=workspace_config,
            fs=fs,
            subprocess_runner=subprocess,
            manifest_loader=manifest_loader,
        ),
        extension_exclude_svc=ExtensionExcludeService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        extension_claudemd_svc=ExtensionClaudemdService(
            config=workspace_config,
            fs=fs,
        ),
        agents_md_svc=AgentsMdService(
            config=workspace_config,
            fs=fs,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=GitOpsService(RepoErrorFactory()),
        registry=registry,
    )


def _destroy_service(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    git: FakeGitRepository,
    registry: IEnvIndexRegistry,
) -> DestroyService:
    hook_svc = ExtensionHookService(
        config=workspace_config,
        fs=fs,
        subprocess_runner=FakeSubprocessRunner(),
        manifest_loader=ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({})),
    )
    return DestroyService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config),
        extension_hook_svc=hook_svc,
        fs=fs,
        git_repo=git,
        registry=registry,
    )


def _project(name: str) -> ProjectRepository:
    return ProjectRepository(
        name=name,
        main_path=WORKSPACE_ROOT / "projects" / name,
        main_branch="main",
    )


def _workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


# ---------------------------------------------------------------------------
# (a) init records the allocated index + .winter.env matches
# ---------------------------------------------------------------------------


class TestInitRecordsRegistry:
    def test_init_records_index_for_alias_env(self) -> None:
        """reconcile_env for an alias name (alpha) records index 1 in the registry."""
        demo_path = WORKSPACE_ROOT / "projects" / "demo"
        fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
        fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
        fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
        git = FakeGitRepository()
        git.local_branches[demo_path] = ["main"]

        cfg = _default_config()
        registry = _InMemoryRegistry()
        svc = _init_service(cfg, fs, FakeSubprocessRunner(), git, registry)
        ok = svc.reconcile_env("alpha", FakeInitReporter())

        assert ok is True
        assert registry.get_index("alpha") == 1

    def test_init_env_file_index_matches_registry(self) -> None:
        """.winter.env WINTER_ENV_INDEX matches what the registry recorded."""
        demo_path = WORKSPACE_ROOT / "projects" / "demo"
        fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
        fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
        fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
        git = FakeGitRepository()
        git.local_branches[demo_path] = ["main"]

        cfg = _default_config()
        registry = _InMemoryRegistry()
        svc = _init_service(cfg, fs, FakeSubprocessRunner(), git, registry)
        svc.reconcile_env("alpha", FakeInitReporter())

        recorded_index = registry.get_index("alpha")
        assert recorded_index is not None

        env_file = WORKSPACE_ROOT / "alpha" / ".winter.env"
        content = fs.files[env_file]
        assert f"WINTER_ENV_INDEX={recorded_index}" in content

    def test_init_env_file_port_base_matches_port_base_for_index(self) -> None:
        """.winter.env WINTER_PORT_BASE == config.port_base_for_index(registry_index)."""
        demo_path = WORKSPACE_ROOT / "projects" / "demo"
        fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
        fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
        fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
        git = FakeGitRepository()
        git.local_branches[demo_path] = ["main"]

        cfg = _default_config()
        registry = _InMemoryRegistry()
        svc = _init_service(cfg, fs, FakeSubprocessRunner(), git, registry)
        svc.reconcile_env("alpha", FakeInitReporter())

        recorded_index = registry.get_index("alpha")
        assert recorded_index is not None
        expected_port_base = cfg.port_base_for_index(recorded_index)

        env_file = WORKSPACE_ROOT / "alpha" / ".winter.env"
        content = fs.files[env_file]
        assert f"WINTER_PORT_BASE={expected_port_base}" in content

    def test_init_idempotent_reuses_recorded_index(self) -> None:
        """A second reconcile_env reuses the registry-recorded index (no collision probe)."""
        demo_path = WORKSPACE_ROOT / "projects" / "demo"
        fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
        fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
        fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
        git = FakeGitRepository()
        git.local_branches[demo_path] = ["main"]

        cfg = _default_config()
        registry = _InMemoryRegistry()
        svc = _init_service(cfg, fs, FakeSubprocessRunner(), git, registry)

        svc.reconcile_env("alpha", FakeInitReporter())
        first_index = registry.get_index("alpha")

        # Simulate worktree already existing on second run.
        svc.reconcile_env("alpha", FakeInitReporter())
        second_index = registry.get_index("alpha")

        assert first_index == second_index == 1


# ---------------------------------------------------------------------------
# (b) destroy removes registry entry; dry-run does not
# ---------------------------------------------------------------------------


class TestDestroyRemovesRegistry:
    def _setup_env_fs(self) -> FakeFilesystem:
        env_root = WORKSPACE_ROOT / "alpha"
        worktree_path = env_root / "demo"
        return FakeFilesystem(
            directories=[WORKSPACE_ROOT / "projects", WORKSPACE_ROOT / "projects" / "demo", env_root, worktree_path],
            files={
                env_root / ".winter.env": "WINTER_ENV=alpha\n",
                WORKSPACE_ROOT / ".git" / "info" / "exclude": "",
            },
        )

    def test_destroy_removes_registry_entry(self) -> None:
        """After destroy, the registry entry for the env is gone."""
        fs = self._setup_env_fs()
        git = FakeGitRepository()
        git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

        cfg = _default_config()
        registry = _InMemoryRegistry()
        registry.assign("alpha", 1)

        svc = _destroy_service(cfg, fs, git, registry)
        ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=FakeInitReporter())

        assert ok is True
        assert registry.get_index("alpha") is None

    def test_destroy_dry_run_does_not_remove_registry_entry(self) -> None:
        """A --dry-run destroy must NOT remove the registry entry."""
        fs = self._setup_env_fs()
        git = FakeGitRepository()
        git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

        cfg = _default_config()
        registry = _InMemoryRegistry()
        registry.assign("alpha", 1)

        svc = _destroy_service(cfg, fs, git, registry)
        ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=True, reporter=FakeInitReporter())

        assert ok is True
        # Registry entry preserved on dry-run.
        assert registry.get_index("alpha") == 1

    def test_destroy_remove_is_noop_when_name_not_registered(self) -> None:
        """If the env was never recorded, remove is a no-op (registry.remove is idempotent)."""
        fs = self._setup_env_fs()
        git = FakeGitRepository()
        git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

        cfg = _default_config()
        registry = _InMemoryRegistry()
        # "alpha" not in registry — remove should not raise.

        svc = _destroy_service(cfg, fs, git, registry)
        ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=FakeInitReporter())

        assert ok is True
        assert registry.get_index("alpha") is None


# ---------------------------------------------------------------------------
# (c) Read path: registry-first, fallback for pre-registry envs
# ---------------------------------------------------------------------------


class TestReadPathRegistryLookup:
    def test_read_returns_registry_index_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the registry has an entry, _build_environment uses it."""
        registry = _InMemoryRegistry()
        registry.assign("alpha", 7)  # unusual index — proves registry is used

        repo = ReadWorkspaceRepository(
            error_factory=RepoErrorFactory(),
            env_aliases=["alpha", "beta"],
            envs_per_workspace=20,
            registry=registry,
        )

        workspace = _workspace()
        # Patch Path.is_dir / Path.iterdir to avoid filesystem hit.
        monkeypatch.setattr(Path, "is_dir", lambda self: True)

        env = repo.get_environment(workspace, "alpha")
        assert env.index == 7

    def test_read_falls_back_to_resolve_when_not_in_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the registry has no entry (pre-registry env), resolve_env_index is used."""
        registry = _InMemoryRegistry()
        # No entry for "alpha" — fall back to resolve.

        repo = ReadWorkspaceRepository(
            error_factory=RepoErrorFactory(),
            env_aliases=["alpha", "beta"],
            envs_per_workspace=20,
            registry=registry,
        )

        workspace = _workspace()
        monkeypatch.setattr(Path, "is_dir", lambda self: True)

        env = repo.get_environment(workspace, "alpha")
        # resolve_env_index("alpha", ["alpha", "beta"], 20) == 1
        assert env.index == resolve_env_index("alpha", ["alpha", "beta"], 20)

    def test_read_without_registry_uses_resolve_env_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no registry is injected (registry=None), resolve_env_index is always used."""
        repo = ReadWorkspaceRepository(
            error_factory=RepoErrorFactory(),
            env_aliases=["alpha", "beta"],
            envs_per_workspace=20,
            registry=None,
        )

        workspace = _workspace()
        monkeypatch.setattr(Path, "is_dir", lambda self: True)

        env = repo.get_environment(workspace, "alpha")
        assert env.index == resolve_env_index("alpha", ["alpha", "beta"], 20)

    def test_read_registry_index_overrides_hash_suggestion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ad-hoc env whose registry index differs from its hash suggestion uses the registry."""
        registry = _InMemoryRegistry()
        # Assign "feature-x" a specific index (simulating probed-away from its hash suggestion).
        registry.assign("feature-x", 15)

        repo = ReadWorkspaceRepository(
            error_factory=RepoErrorFactory(),
            env_aliases=["alpha"],
            envs_per_workspace=20,
            registry=registry,
        )

        workspace = _workspace()
        monkeypatch.setattr(Path, "is_dir", lambda self: True)

        env = repo.get_environment(workspace, "feature-x")
        assert env.index == 15


# ---------------------------------------------------------------------------
# (d) End-to-end: real TomlEnvIndexRegistry persisted across init + destroy
# ---------------------------------------------------------------------------


class TestEndToEndWithTomlRegistry:
    def test_init_persists_to_state_toml_read_back_by_fresh_registry(self, tmp_path: Path) -> None:
        """init_service writes the index to state.toml; a fresh TomlEnvIndexRegistry reads it back."""
        state_path = tmp_path / ".winter" / "state.toml"
        real_fs = LocalFilesystem()
        registry = TomlEnvIndexRegistry(state_path, real_fs)

        demo_path = WORKSPACE_ROOT / "projects" / "demo"
        fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
        fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
        fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
        git = FakeGitRepository()
        git.local_branches[demo_path] = ["main"]

        cfg = _default_config()
        svc = _init_service(cfg, fs, FakeSubprocessRunner(), git, registry)
        ok = svc.reconcile_env("alpha", FakeInitReporter())
        assert ok is True

        # Fresh registry instance reads back the persisted entry.
        fresh = TomlEnvIndexRegistry(state_path, real_fs)
        assert fresh.get_index("alpha") == 1

    def test_destroy_removes_from_state_toml(self, tmp_path: Path) -> None:
        """destroy_service removes the registry entry from state.toml."""
        state_path = tmp_path / ".winter" / "state.toml"
        real_fs = LocalFilesystem()
        registry = TomlEnvIndexRegistry(state_path, real_fs)
        registry.assign("alpha", 1)

        env_root = WORKSPACE_ROOT / "alpha"
        worktree_path = env_root / "demo"
        fs = FakeFilesystem(
            directories=[WORKSPACE_ROOT / "projects", WORKSPACE_ROOT / "projects" / "demo", env_root, worktree_path],
            files={
                env_root / ".winter.env": "WINTER_ENV=alpha\n",
                WORKSPACE_ROOT / ".git" / "info" / "exclude": "",
            },
        )
        git = FakeGitRepository()
        git.clean_worktrees.add(worktree_path)

        cfg = _default_config()
        svc = _destroy_service(cfg, fs, git, registry)
        ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=FakeInitReporter())
        assert ok is True

        fresh = TomlEnvIndexRegistry(state_path, real_fs)
        assert fresh.get_index("alpha") is None


# ---------------------------------------------------------------------------
# (e) M2: TomlEnvIndexRegistry against in-memory fake filesystem
# ---------------------------------------------------------------------------


class TestTomlEnvIndexRegistryWithFakeFilesystem:
    """Verify TomlEnvIndexRegistry routes all I/O through the injected filesystem seam.

    Tests run against FakeFilesystem (the in-memory fake used across the suite)
    to prove the adapter never touches real disk paths.
    """

    def _registry(self, fake_fs: FakeFilesystem, state_path: Path) -> TomlEnvIndexRegistry:
        return TomlEnvIndexRegistry(state_path, fake_fs)

    def test_empty_registry_returns_none(self) -> None:
        """get_index on a missing state file returns None."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)
        assert reg.get_index("alpha") is None

    def test_assign_writes_through_fake_fs(self) -> None:
        """assign() writes TOML into the fake filesystem, not real disk."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)

        reg.assign("alpha", 1)

        assert state_path in fake_fs.files
        assert "alpha" in fake_fs.files[state_path]

    def test_assign_then_get_index_roundtrip(self) -> None:
        """Values written via assign() are readable back via get_index()."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)

        reg.assign("alpha", 1)
        reg.assign("feature-x", 14)

        assert reg.get_index("alpha") == 1
        assert reg.get_index("feature-x") == 14

    def test_all_assignments_returns_all_entries(self) -> None:
        """all_assignments() returns every entry written via assign()."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)

        reg.assign("alpha", 1)
        reg.assign("beta", 2)

        result = reg.all_assignments()
        assert result == {"alpha": 1, "beta": 2}

    def test_remove_deletes_entry(self) -> None:
        """remove() deletes an existing entry; get_index returns None afterwards."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)

        reg.assign("alpha", 1)
        reg.remove("alpha")

        assert reg.get_index("alpha") is None

    def test_remove_missing_entry_is_noop(self) -> None:
        """remove() on a name that was never assigned does not raise."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        reg = self._registry(fake_fs, state_path)
        reg.remove("nonexistent")  # must not raise

    def test_mkdir_called_via_fake_fs_on_first_write(self) -> None:
        """The parent directory is created through the seam (fake_fs.mkdir), not raw Path.mkdir."""
        fake_fs = FakeFilesystem()
        state_path = Path("/ws/.winter/state.toml")
        # Parent NOT pre-created — the seam must create it.
        reg = self._registry(fake_fs, state_path)

        reg.assign("alpha", 1)

        assert state_path.parent in fake_fs.directories
