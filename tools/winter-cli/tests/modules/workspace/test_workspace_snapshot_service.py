from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.config.models import (
    AdoptExtensions,
    DashboardLayout,
    ProjectRepositoryConfig,
    SingletonRepository,
    SingletonType,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureWorktree,
    ProjectRepository,
    RepoCommit,
    RepoError,
    RepoStatus,
    StandaloneRepository,
    StandaloneRepoStatus,
    Workspace,
)
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
from winter_cli.modules.workspace.prune_service import PruneOrphan
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService

WORKSPACE_ROOT = Path("/ws")

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git"),
            ProjectRepositoryConfig(name="repo-b", url="git@example.com:org/repo-b.git"),
        ],
        standalone_repos=[],
    )


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeReadWorkspaceRepository:
    """Stub for `IReadWorkspaceRepository` — returns canned environments."""

    def __init__(
        self,
        envs: list[FeatureEnvironment] | None = None,
        feature_branch: str | None = None,
        env_errors: dict[str, RepoError] | None = None,
    ) -> None:
        self._envs: list[FeatureEnvironment] = envs or []
        self._feature_branch = feature_branch
        self._env_errors: dict[str, RepoError] = env_errors or {}

    def get_environments(
        self, workspace: Workspace, project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        return list(self._envs)

    def get_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        return FeatureEnvironment(workspace=workspace, name=name, index=1, path=workspace.root_path / name)

    def get_environment_status(
        self, env: FeatureEnvironment, project_repos: list[ProjectRepository]
    ) -> FeatureEnvironmentStatus:
        if env.name in self._env_errors:
            raise self._env_errors[env.name]
        return FeatureEnvironmentStatus(environment=env, feature_branch=self._feature_branch)


class FakeRepoRepository:
    """Stub for `IWriteRepoRepository` — returns canned `RepoStatus` per worktree.

    Maps repo name → `RepoStatus` to return, or `RepoError` to raise.
    `get_project_status` maps repo name → `RepoStatus` for source-checkout reads.
    Unknown attribute access fails loudly so accidental fan-out surfaces.
    """

    def __init__(
        self,
        worktree_statuses: dict[str, RepoStatus] | None = None,
        project_statuses: dict[str, RepoStatus] | None = None,
        errors: dict[str, RepoError] | None = None,
        standalone_statuses: dict[str, StandaloneRepoStatus] | None = None,
    ) -> None:
        self._worktree_statuses: dict[str, RepoStatus] = worktree_statuses or {}
        self._project_statuses: dict[str, RepoStatus] = project_statuses or {}
        self._errors: dict[str, RepoError] = errors or {}
        self._standalone_statuses: dict[str, StandaloneRepoStatus] = standalone_statuses or {}

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        name = worktree.repository.name
        if name in self._errors:
            raise self._errors[name]
        return self._worktree_statuses[name]

    def get_project_status(self, repo: ProjectRepository) -> RepoStatus:
        name = repo.name
        if name in self._errors:
            raise self._errors[name]
        if name in self._project_statuses:
            return self._project_statuses[name]
        # Default: clean status when not explicitly configured.
        return RepoStatus(
            name=name,
            path=str(WORKSPACE_ROOT / "projects" / name),
            main_branch="main",
            branch="main",
            ahead=0,
            behind=0,
            dirty_files=[],
        )

    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        name = repo.name
        if name in self._errors:
            raise self._errors[name]
        if name in self._standalone_statuses:
            return self._standalone_statuses[name]
        # Default: a "not present"-style clean status, mirroring the real adapter's
        # behavior for a missing/empty standalone (name preserved, no divergence).
        return StandaloneRepoStatus(repository=repo)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeRepoRepository.{name} called unexpectedly")


class FakePruneService:
    """Stub for `PruneService` — returns canned orphan list."""

    def __init__(self, orphans: list[PruneOrphan] | None = None) -> None:
        self._orphans = orphans or []

    def find_orphans(self) -> list[PruneOrphan]:
        return list(self._orphans)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakePruneService.{name} called unexpectedly")


class FakeConfigLockRepository:
    """Stub for `IConfigLockRepository` — returns canned lock entries."""

    def __init__(self, entries: dict[str, LockEntry] | None = None) -> None:
        self._entries: dict[str, LockEntry] = entries or {}

    def read(self) -> dict[str, LockEntry]:
        return dict(self._entries)

    def write(self, entries: Any) -> None:
        pass


class FakeGitRepositoryForSnapshot:
    """Stub for `IGitRepository` — returns canned HEAD commits."""

    def __init__(self, head_commits: dict[str, str] | None = None) -> None:
        self._head_commits: dict[str, str] = head_commits or {}

    def get_head_commit(self, path: Path) -> str:
        name = path.name
        if name in self._head_commits:
            return self._head_commits[name]
        raise RuntimeError(f"no head_commit configured for {path}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeGitRepositoryForSnapshot.{name} called unexpectedly")


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_env(workspace: Workspace, name: str, index: int) -> FeatureEnvironment:
    return FeatureEnvironment(workspace=workspace, name=name, index=index, path=workspace.root_path / name)


def _make_repo(name: str) -> ProjectRepository:
    return ProjectRepository(
        name=name,
        main_path=WORKSPACE_ROOT / "projects" / name,
        main_branch="main",
    )


def _clean_repo_status(name: str, *, env_name: str = "alpha") -> RepoStatus:
    """A fully-clean `RepoStatus` for worktree assertions."""
    return RepoStatus(
        name=name,
        path=f"{WORKSPACE_ROOT}/{env_name}/{name}",
        main_branch="main",
        branch=env_name,
        ahead=0,
        behind=0,
        dirty_files=[],
        staged_count=0,
        unstaged_count=0,
        untracked_count=0,
        recent_commits=[],
        tracking_branch="origin/feature/x",
        tracking_ahead=0,
        tracking_behind=0,
        tracking_ref_present=True,
    )


def _dirty_repo_status(
    name: str,
    *,
    env_name: str = "alpha",
    staged: int = 0,
    unstaged: int = 0,
    untracked: int = 0,
    ahead: int = 0,
    behind: int = 0,
    commit_subject: str | None = None,
) -> RepoStatus:
    dirty_files = [f"f{i}" for i in range(staged + unstaged + untracked)]
    recent_commits = [RepoCommit(short_hash="abc1234", message=commit_subject)] if commit_subject else []
    return RepoStatus(
        name=name,
        path=f"{WORKSPACE_ROOT}/{env_name}/{name}",
        main_branch="main",
        branch=env_name,
        ahead=ahead,
        behind=behind,
        dirty_files=dirty_files,
        staged_count=staged,
        unstaged_count=unstaged,
        untracked_count=untracked,
        recent_commits=recent_commits,
        tracking_branch="origin/feature/x",
        tracking_ahead=ahead,
        tracking_behind=0,
        tracking_ref_present=True,
    )


def _service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    *,
    envs: list[FeatureEnvironment] | None = None,
    feature_branch: str | None = None,
    worktree_statuses: dict[str, RepoStatus] | None = None,
    project_statuses: dict[str, RepoStatus] | None = None,
    repo_errors: dict[str, RepoError] | None = None,
    env_errors: dict[str, RepoError] | None = None,
    standalone_statuses: dict[str, StandaloneRepoStatus] | None = None,
    projects_on_disk: list[str] | None = None,
    orphans: list[PruneOrphan] | None = None,
    lock_entries: dict[str, LockEntry] | None = None,
    head_commits: dict[str, str] | None = None,
    dashboard_layout: DashboardLayout = DashboardLayout.auto,
) -> WorkspaceSnapshotService:
    """Construct a `WorkspaceSnapshotService` with all fakes wired."""
    from tests.conftest import ClickRecorder, FakeFilesystem

    worktree_repo = FakeReadWorkspaceRepository(envs=envs, feature_branch=feature_branch, env_errors=env_errors)
    repo_repo = FakeRepoRepository(
        worktree_statuses=worktree_statuses,
        project_statuses=project_statuses,
        errors=repo_errors,
        standalone_statuses=standalone_statuses,
    )
    repo_factory = RepositoryFactory(workspace_config)

    # Wire a FakeFilesystem that shows whatever repos are "on disk" under projects/
    on_disk = projects_on_disk or [r.name for r in repo_factory.get_project_repos()]
    dirs = [WORKSPACE_ROOT / "projects"]
    for name in on_disk:
        dirs.append(WORKSPACE_ROOT / "projects" / name)
    fs = FakeFilesystem(directories=dirs)
    click_rec = ClickRecorder()

    drift_svc = DriftWarningService(
        workspace=workspace,
        repo_factory=repo_factory,
        fs=fs,
        click=click_rec,
    )
    prune_svc = FakePruneService(orphans=orphans)  # type: ignore[arg-type]
    config_lock_repo = FakeConfigLockRepository(entries=lock_entries)
    git_repo = FakeGitRepositoryForSnapshot(head_commits=head_commits)

    env_status_svc = EnvStatusService(
        worktree_repo=worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
    )

    return WorkspaceSnapshotService(
        workspace=workspace,
        env_status_svc=env_status_svc,
        workspace_repo=worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=repo_factory,
        drift_warning_svc=drift_svc,
        prune_svc=prune_svc,  # type: ignore[arg-type]
        config_lock_repo=config_lock_repo,  # type: ignore[arg-type]
        git_repo=git_repo,  # type: ignore[arg-type]
        dashboard_layout=dashboard_layout,
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_collect_empty_workspace_returns_empty_envs(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    svc = _service(workspace, workspace_config, envs=[])

    snapshot = svc.collect()

    assert snapshot.schema_version == 1
    assert snapshot.environments == []
    assert snapshot.workspace.root_path == str(WORKSPACE_ROOT)
    assert snapshot.workspace.extensions == []
    assert snapshot.workspace.orphans == []
    assert snapshot.workspace.drift_missing == []
    assert snapshot.workspace.drift_undeclared == []


def test_collect_no_patterns_returns_all_envs(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    alpha = _make_env(workspace, "alpha", 1)
    beta = _make_env(workspace, "beta", 2)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha, beta],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    assert len(snapshot.environments) == 2
    assert snapshot.environments[0].name == "alpha"
    assert snapshot.environments[1].name == "beta"


def test_collect_bare_env_pattern_returns_that_envs_worktrees(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """A bare env pattern like 'alpha' expands to 'alpha/*' — returns all of alpha's worktrees."""
    alpha = _make_env(workspace, "alpha", 1)
    beta = _make_env(workspace, "beta", 2)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha, beta],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect(patterns=["alpha"])

    assert len(snapshot.environments) == 1
    assert snapshot.environments[0].name == "alpha"
    assert len(snapshot.environments[0].worktrees) == 2


def test_collect_env_repo_pattern_returns_single_worktree(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """'alpha/repo-a' returns only that one worktree under alpha."""
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect(patterns=["alpha/repo-a"])

    assert len(snapshot.environments) == 1
    assert snapshot.environments[0].name == "alpha"
    assert len(snapshot.environments[0].worktrees) == 1
    assert snapshot.environments[0].worktrees[0].repo == "repo-a"


def test_collect_wildcard_env_pattern_returns_matching_worktree_across_all_envs(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """'*/repo-a' returns the repo-a worktree from every env that has it."""
    alpha = _make_env(workspace, "alpha", 1)
    beta = _make_env(workspace, "beta", 2)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha, beta],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect(patterns=["*/repo-a"])

    # Both envs have repo-a (same worktree_statuses used for both)
    assert len(snapshot.environments) == 2
    for env_snap in snapshot.environments:
        assert len(env_snap.worktrees) == 1
        assert env_snap.worktrees[0].repo == "repo-a"


def test_collect_glob_env_pattern_works(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """'alpha/*' (explicit glob) returns all alpha worktrees."""
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect(patterns=["alpha/*"])

    assert len(snapshot.environments) == 1
    assert len(snapshot.environments[0].worktrees) == 2


def test_collect_zero_match_pattern_raises_click_exception(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """A pattern that matches no worktree raises ClickException with the pattern name."""
    import click

    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    with pytest.raises(click.ClickException, match="nope"):
        svc.collect(patterns=["nope"])


def test_collect_port_base_derived_from_index(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    env_snap = snapshot.environments[0]
    assert env_snap.index == 1
    assert env_snap.port_base == workspace.base_port + 1 * workspace.ports_per_env


def test_collect_dirty_worktree_counts_surface_correctly(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _dirty_repo_status("repo-a", staged=2, unstaged=3, untracked=1),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    env_snap = snapshot.environments[0]
    assert len(env_snap.worktrees) == 2

    dirty_wt = next(wt for wt in env_snap.worktrees if wt.repo == "repo-a")
    assert dirty_wt.staged == 2
    assert dirty_wt.unstaged == 3
    assert dirty_wt.untracked == 1
    # dirty = total unique dirty files = staged + unstaged + untracked (no overlap in fake)
    assert dirty_wt.dirty == 6

    clean_wt = next(wt for wt in env_snap.worktrees if wt.repo == "repo-b")
    assert clean_wt.staged == 0
    assert clean_wt.unstaged == 0
    assert clean_wt.untracked == 0
    assert clean_wt.dirty == 0


def test_collect_last_commit_subject_populated(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _dirty_repo_status("repo-a", ahead=1, commit_subject="feat: add thing"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    wt_a = next(wt for wt in snapshot.environments[0].worktrees if wt.repo == "repo-a")
    assert wt_a.last_commit_subject == "feat: add thing"

    wt_b = next(wt for wt in snapshot.environments[0].worktrees if wt.repo == "repo-b")
    assert wt_b.last_commit_subject is None


def test_collect_drifted_source_checkout_surfaces_behind_origin(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """A source checkout that is behind origin surfaces in source_checkouts."""
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    # repo-a is behind origin on main branch
    project_statuses = {
        "repo-a": RepoStatus(
            name="repo-a",
            path=str(WORKSPACE_ROOT / "projects" / "repo-a"),
            main_branch="main",
            branch="main",
            ahead=0,
            behind=3,
            dirty_files=[],
            staged_count=0,
            unstaged_count=0,
            untracked_count=0,
            tracking_branch="origin/main",
            tracking_ahead=0,
            tracking_behind=3,
            tracking_ref_present=True,
        ),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
        project_statuses=project_statuses,
    )

    snapshot = svc.collect()

    # repo-a has behind=3, so it should appear in source_checkouts
    repo_a_checkout = next((sc for sc in snapshot.source_checkouts if sc.repo == "repo-a"), None)
    assert repo_a_checkout is not None
    assert repo_a_checkout.behind_origin == 3
    assert repo_a_checkout.ahead_origin == 0


def test_collect_orphan_detection_populates_orphan_list(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    orphans = [
        PruneOrphan(
            kind="project_clone",
            path=WORKSPACE_ROOT / "projects" / "ghost",
            safe_to_remove=True,
            notes="",
        ),
        PruneOrphan(
            kind="broken_symlink",
            path=WORKSPACE_ROOT / ".claude" / "skills" / "dead-link",
            safe_to_remove=True,
            notes="",
        ),
    ]
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
        orphans=orphans,
    )

    snapshot = svc.collect()

    assert len(snapshot.workspace.orphans) == 2
    kinds = {o.kind for o in snapshot.workspace.orphans}
    assert kinds == {"project_clone", "broken_symlink"}
    for o in snapshot.workspace.orphans:
        assert o.safe_to_remove is True


def test_collect_drift_missing_populates_workspace_level(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """When a declared repo is absent from disk, drift_missing includes its name."""
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    # Only repo-a is on disk; repo-b is missing
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
        projects_on_disk=["repo-a"],
    )

    snapshot = svc.collect()

    assert "repo-b" in snapshot.workspace.drift_missing


def test_collect_extensions_lists_declared_standalones_without_probing(workspace: Workspace) -> None:
    """`collect().workspace.extensions` is a pure name read of the declared standalones.

    It must NOT git-probe them: a broken standalone repo (here, wired to raise
    on `get_standalone_status`) must not fail `ws status` / `--json` just to list
    the extension's name. The dashboard, which needs each standalone's health,
    probes separately — see `test_collect_for_dashboard_probes_singletons_and_standalones`.
    """
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git")],
        standalone_repos=[
            StandaloneRepositoryConfig(name="ext-a", url="git@example.com:org/ext-a.git"),
            StandaloneRepositoryConfig(name="ext-b", url="git@example.com:org/ext-b.git"),
        ],
    )
    alpha = _make_env(workspace, "alpha", 1)
    svc = _service(
        workspace,
        config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={"repo-a": _clean_repo_status("repo-a")},
        # If collect() probed standalones, these would raise and fail the call.
        repo_errors={"ext-a": RepoError("ext-a would explode if probed"), "ext-b": RepoError("boom")},
    )

    snapshot = svc.collect()

    assert sorted(snapshot.workspace.extensions) == ["ext-a", "ext-b"]


def test_collect_on_repo_error_callback_skips_failed_worktree(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """When on_repo_error is provided, a failing worktree is skipped."""
    alpha = _make_env(workspace, "alpha", 1)
    # repo-a will fail, repo-b will succeed
    worktree_statuses = {
        "repo-b": _clean_repo_status("repo-b"),
    }
    repo_errors = {
        "repo-a": RepoError("repo-a exploded"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
        repo_errors=repo_errors,
    )

    reported: list[tuple[str, str]] = []

    def on_error(wt: FeatureWorktree, exc: RepoError) -> None:
        reported.append((wt.repository.name, str(exc)))

    snapshot = svc.collect(on_repo_error=on_error)

    env_snap = snapshot.environments[0]
    assert len(env_snap.worktrees) == 1
    assert env_snap.worktrees[0].repo == "repo-b"
    assert len(reported) == 1
    assert reported[0][0] == "repo-a"


def test_collect_propagates_repo_error_without_callback(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """Without on_repo_error, a failing worktree's error propagates."""
    alpha = _make_env(workspace, "alpha", 1)
    repo_errors = {"repo-a": RepoError("repo-a boom")}
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={},
        repo_errors=repo_errors,
    )

    with pytest.raises(RepoError, match="repo-a boom"):
        svc.collect()


def test_collect_propagates_env_error_without_callback(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """Without on_repo_error, an env-LEVEL RepoError propagates from collect().

    Locks the invariant the `overview is None` branch in collect() documents:
    the CLI path passes on_repo_error=None, so an env-level probe failure is
    never silently skipped — it surfaces and the command exits non-zero.
    """
    alpha = _make_env(workspace, "alpha", 1)
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={"repo-a": _clean_repo_status("repo-a"), "repo-b": _clean_repo_status("repo-b")},
        env_errors={"alpha": RepoError("alpha env boom")},
    )

    with pytest.raises(RepoError, match="alpha env boom"):
        svc.collect()


def test_collect_pinned_surfaces_in_worktree_snapshot(workspace: Workspace) -> None:
    """WorktreeSnapshot.pinned reflects the underlying ProjectRepository.pinned value."""
    pinned_config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git", pinned=True),
            ProjectRepositoryConfig(name="repo-b", url="git@example.com:org/repo-b.git"),
        ],
        standalone_repos=[],
    )
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        pinned_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    env_snap = snapshot.environments[0]
    wt_a = next(wt for wt in env_snap.worktrees if wt.repo == "repo-a")
    wt_b = next(wt for wt in env_snap.worktrees if wt.repo == "repo-b")
    assert wt_a.pinned is True
    assert wt_b.pinned is False


# ── collect_for_dashboard ──────────────────────────────────────────────────────


def test_collect_for_dashboard_skips_env_whose_probe_fails_with_callback(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """An env-level RepoError is tolerated (env skipped) when on_repo_error is supplied."""
    alpha = _make_env(workspace, "alpha", 1)
    beta = _make_env(workspace, "beta", 2)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha, beta],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
        env_errors={"alpha": RepoError("alpha env exploded")},
    )

    reported: list[str] = []
    data = svc.collect_for_dashboard(on_repo_error=lambda wt, exc: reported.append(wt.repository.name))

    # alpha's env-level probe failed and was tolerated; only beta survives.
    names = [o.status.environment.name for o in data.overviews]
    assert names == ["beta"]


def test_collect_for_dashboard_propagates_env_error_without_callback(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """Without on_repo_error, an env-level RepoError propagates — same policy as collect()."""
    alpha = _make_env(workspace, "alpha", 1)
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={"repo-a": _clean_repo_status("repo-a"), "repo-b": _clean_repo_status("repo-b")},
        env_errors={"alpha": RepoError("alpha env boom")},
    )

    with pytest.raises(RepoError, match="alpha env boom"):
        svc.collect_for_dashboard()


def test_collect_for_dashboard_probes_singletons_and_standalones(workspace: Workspace) -> None:
    """standalone_statuses covers both implicit singletons and declared standalones."""
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git")],
        standalone_repos=[StandaloneRepositoryConfig(name="ext-a", url="git@example.com:org/ext-a.git")],
        singleton_repos=[SingletonRepository(name="workspace", type=SingletonType.workspace)],
    )
    alpha = _make_env(workspace, "alpha", 1)
    standalone_statuses = {
        "workspace": StandaloneRepoStatus(repository=StandaloneRepository(name="workspace", path=WORKSPACE_ROOT)),
        "ext-a": StandaloneRepoStatus(
            repository=StandaloneRepository(name="ext-a", path=WORKSPACE_ROOT / "ext-a"),
            dirty_count=2,
        ),
    }
    svc = _service(
        workspace,
        config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={"repo-a": _clean_repo_status("repo-a")},
        standalone_statuses=standalone_statuses,
    )

    data = svc.collect_for_dashboard()

    names = sorted(s.name for s in data.standalone_statuses)
    assert names == ["ext-a", "workspace"]


def test_collect_for_dashboard_populates_main_branch_statuses(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """A diverged source checkout surfaces in main_statuses; clean ones are omitted."""
    alpha = _make_env(workspace, "alpha", 1)
    project_statuses = {
        "repo-a": RepoStatus(
            name="repo-a",
            path=str(WORKSPACE_ROOT / "projects" / "repo-a"),
            main_branch="main",
            branch="main",
            ahead=0,
            behind=2,
            dirty_files=[],
            tracking_branch="origin/main",
            tracking_behind=2,
            tracking_ref_present=True,
        ),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses={"repo-a": _clean_repo_status("repo-a"), "repo-b": _clean_repo_status("repo-b")},
        project_statuses=project_statuses,
    )

    data = svc.collect_for_dashboard()

    assert "repo-a" in data.main_statuses
    assert data.main_statuses["repo-a"].behind == 2
    # repo-b is clean (default project status) → omitted from main_statuses.
    assert "repo-b" not in data.main_statuses


# ── C4: standalone_pins in ws status --json ───────────────────────────────────


def _config_with_standalones(
    refs: dict[str, str | None],
) -> WorkspaceConfig:
    """Build a WorkspaceConfig with named standalone repos, optionally pinned."""
    standalone_repos = [
        StandaloneRepositoryConfig(name=name, url=f"git@example.com:org/{name}.git", ref=ref)
        for name, ref in refs.items()
    ]
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git"),
        ],
        standalone_repos=standalone_repos,
    )


def test_collect_standalone_pins_empty_when_no_ref(workspace: Workspace) -> None:
    """Standalones without a ref produce no pin snapshot entries."""
    config = _config_with_standalones({"ext-a": None})
    svc = _service(workspace, config)

    snapshot = svc.collect()

    assert snapshot.workspace.standalone_pins == []


def test_collect_standalone_pins_present_when_ref_configured(workspace: Workspace) -> None:
    """A standalone with a ref but no lock entry appears with kind/locked_commit=None."""
    config = _config_with_standalones({"ext-a": "v1.2.3"})
    # No lock entries, no HEAD commit configured → probes fail gracefully.
    svc = _service(workspace, config, lock_entries={})

    snapshot = svc.collect()

    pins = snapshot.workspace.standalone_pins
    assert len(pins) == 1
    pin = pins[0]
    assert pin.name == "ext-a"
    assert pin.ref == "v1.2.3"
    assert pin.kind is None
    assert pin.locked_commit is None
    assert pin.config_ref_drift is False
    assert pin.head_drift is False
    assert pin.head_commit is None


def test_collect_standalone_pins_no_drift_when_locked_and_head_matches(workspace: Workspace) -> None:
    """When the locked commit matches HEAD and config ref matches lock ref → no drift."""
    sha = "a" * 40
    config = _config_with_standalones({"ext-a": "main"})
    lock_entries = {
        "ext-a": LockEntry(name="ext-a", ref="main", kind=RefKind.branch, commit=sha),
    }
    head_commits = {"ext-a": sha}
    svc = _service(workspace, config, lock_entries=lock_entries, head_commits=head_commits)

    snapshot = svc.collect()

    pin = snapshot.workspace.standalone_pins[0]
    assert pin.name == "ext-a"
    assert pin.ref == "main"
    assert pin.kind == "branch"
    assert pin.locked_commit == sha
    assert pin.head_commit == sha
    assert pin.config_ref_drift is False
    assert pin.head_drift is False


def test_collect_standalone_pins_head_drift_when_head_differs_from_lock(workspace: Workspace) -> None:
    """When HEAD differs from the locked commit → head_drift=True."""
    locked_sha = "a" * 40
    current_sha = "b" * 40
    config = _config_with_standalones({"ext-a": "main"})
    lock_entries = {
        "ext-a": LockEntry(name="ext-a", ref="main", kind=RefKind.branch, commit=locked_sha),
    }
    head_commits = {"ext-a": current_sha}
    svc = _service(workspace, config, lock_entries=lock_entries, head_commits=head_commits)

    snapshot = svc.collect()

    pin = snapshot.workspace.standalone_pins[0]
    assert pin.head_drift is True
    assert pin.locked_commit == locked_sha
    assert pin.head_commit == current_sha
    assert pin.config_ref_drift is False


def test_collect_standalone_pins_config_ref_drift_when_lock_ref_differs(workspace: Workspace) -> None:
    """When the config ref changed since the lock was written → config_ref_drift=True."""
    sha = "c" * 40
    config = _config_with_standalones({"ext-a": "v2.0.0"})  # config now says v2.0.0
    lock_entries = {
        "ext-a": LockEntry(name="ext-a", ref="v1.0.0", kind=RefKind.tag, commit=sha),
    }
    head_commits = {"ext-a": sha}
    svc = _service(workspace, config, lock_entries=lock_entries, head_commits=head_commits)

    snapshot = svc.collect()

    pin = snapshot.workspace.standalone_pins[0]
    assert pin.config_ref_drift is True
    assert pin.ref == "v2.0.0"
    assert pin.locked_commit == sha
    assert pin.head_drift is False


def test_collect_standalone_pins_only_includes_repos_with_ref(workspace: Workspace) -> None:
    """Mixed standalones: only those with a ref appear in standalone_pins."""
    sha = "d" * 40
    config = _config_with_standalones({"pinned-ext": "main", "unpinned-ext": None})
    lock_entries = {
        "pinned-ext": LockEntry(name="pinned-ext", ref="main", kind=RefKind.branch, commit=sha),
    }
    head_commits = {"pinned-ext": sha}
    svc = _service(workspace, config, lock_entries=lock_entries, head_commits=head_commits)

    snapshot = svc.collect()

    pins = snapshot.workspace.standalone_pins
    assert len(pins) == 1
    assert pins[0].name == "pinned-ext"


# ── dashboard layout block ─────────────────────────────────────────────────────


def _two_repo_worktree_statuses() -> dict[str, RepoStatus]:
    return {
        "repo-a": _clean_repo_status("repo-a", env_name="alpha"),
        "repo-b": _clean_repo_status("repo-b", env_name="alpha"),
    }


def test_dashboard_auto_one_repo_resolves_to_list(workspace: Workspace) -> None:
    """auto boundary: a 1-repo workspace resolves to list regardless of env count."""
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git")],
        standalone_repos=[],
    )
    svc = _service(
        workspace,
        config,
        envs=[_make_env(workspace, "alpha", 1)],
        worktree_statuses={"repo-a": _clean_repo_status("repo-a", env_name="alpha")},
    )

    snapshot = svc.collect()

    assert snapshot.dashboard.configured_layout == "auto"
    assert snapshot.dashboard.resolved_layout == "list"


def test_dashboard_auto_repos_gt_envs_resolves_to_rows(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """auto boundary: 2 repos > 1 env → repos-as-rows."""
    svc = _service(
        workspace,
        workspace_config,
        envs=[_make_env(workspace, "alpha", 1)],
        worktree_statuses=_two_repo_worktree_statuses(),
    )

    snapshot = svc.collect()

    assert snapshot.dashboard.configured_layout == "auto"
    assert snapshot.dashboard.resolved_layout == "repos-as-rows"


def test_dashboard_auto_repos_eq_envs_resolves_to_columns(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """auto boundary: 2 repos == 2 envs → repos-as-columns."""
    svc = _service(
        workspace,
        workspace_config,
        envs=[_make_env(workspace, "alpha", 1), _make_env(workspace, "beta", 2)],
        worktree_statuses=_two_repo_worktree_statuses(),
    )

    snapshot = svc.collect()

    assert snapshot.dashboard.configured_layout == "auto"
    assert snapshot.dashboard.resolved_layout == "repos-as-columns"


def test_dashboard_non_auto_layout_passes_through(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """A configured concrete layout is reported verbatim and is not re-resolved."""
    svc = _service(
        workspace,
        workspace_config,
        envs=[_make_env(workspace, "alpha", 1), _make_env(workspace, "beta", 2)],
        worktree_statuses=_two_repo_worktree_statuses(),
        dashboard_layout=DashboardLayout.list,
    )

    snapshot = svc.collect()

    assert snapshot.dashboard.configured_layout == "list"
    # 2 repos == 2 envs would resolve auto to repos-as-columns; the explicit
    # config wins instead, proving configured layouts are not re-resolved.
    assert snapshot.dashboard.resolved_layout == "list"


def test_dashboard_resolution_ignores_status_patterns(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """Resolution reflects the whole-workspace shape, not the pattern-filtered view."""
    svc = _service(
        workspace,
        workspace_config,
        envs=[_make_env(workspace, "alpha", 1), _make_env(workspace, "beta", 2)],
        worktree_statuses=_two_repo_worktree_statuses(),
    )

    # Scope to a single env/repo — were resolution computed from the filtered
    # snapshot (1 repo, 1 env) it would pick list; the full shape is 2 repos,
    # 2 envs → repos-as-columns.
    snapshot = svc.collect(patterns=["alpha/repo-a"])

    assert len(snapshot.environments) == 1
    assert snapshot.dashboard.resolved_layout == "repos-as-columns"


def test_dashboard_auto_empty_workspace_resolves_to_rows(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """auto with no envs falls back to repos-as-rows, matching the TUI grid's empty guard."""
    svc = _service(workspace, workspace_config, envs=[])

    snapshot = svc.collect()

    assert snapshot.dashboard.configured_layout == "auto"
    assert snapshot.dashboard.resolved_layout == "repos-as-rows"


# ── env decorator / extensions surface ───────────────────────────────────────


def test_collect_env_decorator_populates_env_snapshot_extensions(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """An env decorator that writes into FeatureEnvironmentStatus.extensions is
    serialised into the matching EnvSnapshot.extensions field.

    This is the core Phase-1 Story-1 contract: the JSON status path runs
    environment decorators and their badge strings are available in the
    EnvSnapshot so `_env_snap_to_dict` can emit them.
    """
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    def badge_decorator(status: FeatureEnvironmentStatus, _path: Any) -> None:
        status.extensions["test"] = "● 2/2"

    snapshot = svc.collect(env_decorators=[badge_decorator])

    env_snap = snapshot.environments[0]
    assert env_snap.extensions == {"test": "● 2/2"}


def test_collect_env_decorator_no_decorators_yields_empty_extensions(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """When no decorators are passed, EnvSnapshot.extensions is an empty dict."""
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    snapshot = svc.collect()

    assert snapshot.environments[0].extensions == {}


def test_collect_raising_env_decorator_is_isolated(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """A decorator that raises must not abort the snapshot — the exception is
    swallowed per-decorator, and other decorators still run.

    Mirrors the isolation contract already in get_environment_status for env
    decorators, now exercised end-to-end through collect().
    """
    alpha = _make_env(workspace, "alpha", 1)
    worktree_statuses = {
        "repo-a": _clean_repo_status("repo-a"),
        "repo-b": _clean_repo_status("repo-b"),
    }
    svc = _service(
        workspace,
        workspace_config,
        envs=[alpha],
        feature_branch="feature/x",
        worktree_statuses=worktree_statuses,
    )

    bad_called: list[bool] = []
    good_called: list[bool] = []

    def bad_decorator(status: FeatureEnvironmentStatus, _path: Any) -> None:
        bad_called.append(True)
        raise RuntimeError("decorator exploded")

    def good_decorator(status: FeatureEnvironmentStatus, _path: Any) -> None:
        good_called.append(True)
        status.extensions["ok"] = "running"

    snapshot = svc.collect(env_decorators=[bad_decorator, good_decorator])

    # bad_decorator ran (and was caught), good_decorator ran and wrote its badge.
    assert bad_called == [True]
    assert good_called == [True]
    assert snapshot.environments[0].extensions == {"ok": "running"}
