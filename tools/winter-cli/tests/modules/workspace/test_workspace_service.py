from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    Workspace,
)
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_service import WorkspaceService

WORKSPACE_ROOT = Path("/ws")


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
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
    )


class FakeReadWorkspaceRepository:
    """Stub for the `IReadWorkspaceRepository` Protocol — returns canned status."""

    def __init__(self, feature_branch: str | None = None) -> None:
        self._feature_branch = feature_branch

    def get_environments(
        self, workspace: Workspace, project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        return []

    def get_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        return FeatureEnvironment(workspace=workspace, name=name, index=1, path=workspace.root_path / name)

    def get_environment_status(
        self, env: FeatureEnvironment, project_repos: list[ProjectRepository]
    ) -> FeatureEnvironmentStatus:
        return FeatureEnvironmentStatus(environment=env, feature_branch=self._feature_branch)


class FakeWriteRepoRepository:
    """Stub for the `IWriteRepoRepository` Protocol — records every call."""

    def __init__(self) -> None:
        self.set_upstream_calls: list[tuple[str, str]] = []
        self.set_push_default_calls: list[str] = []
        self.unset_upstream_calls: list[str] = []

    def set_upstream(self, worktree: FeatureWorktree, upstream: str) -> None:
        self.set_upstream_calls.append((worktree.repository.name, upstream))

    def set_push_default(self, worktree: FeatureWorktree) -> None:
        self.set_push_default_calls.append(worktree.repository.name)

    def unset_upstream(self, worktree: FeatureWorktree) -> None:
        self.unset_upstream_calls.append(worktree.repository.name)

    # Methods touched by other WorkspaceService code paths — raise to surface
    # accidental fan-out beyond the call under test.
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


@pytest.fixture
def fake_worktree_repo() -> FakeReadWorkspaceRepository:
    return FakeReadWorkspaceRepository()


@pytest.fixture
def fake_repo_repo() -> FakeWriteRepoRepository:
    return FakeWriteRepoRepository()


@pytest.fixture
def service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    fake_worktree_repo: FakeReadWorkspaceRepository,
    fake_repo_repo: FakeWriteRepoRepository,
) -> WorkspaceService:
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    return WorkspaceService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )


def _env_worktrees(workspace: Workspace, repos: list[ProjectRepository]) -> FeatureEnvironmentWorktrees:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    worktrees = [FeatureWorktree(workspace=workspace, environment=env, repository=r) for r in repos]
    return FeatureEnvironmentWorktrees(environment=env, worktrees=worktrees)


def test_get_feature_environment_worktrees_builds_one_per_repo(workspace: Workspace, service: WorkspaceService) -> None:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    repos = [
        ProjectRepository(name="r1", main_path=workspace.root_path / "projects" / "r1", main_branch="main"),
        ProjectRepository(name="r2", main_path=workspace.root_path / "projects" / "r2", main_branch="main"),
    ]

    env_wts = service.get_feature_environment_worktrees(env, repos)

    assert env_wts.environment is env
    assert [wt.repository.name for wt in env_wts.worktrees] == ["r1", "r2"]
    assert env_wts.worktrees[0].path == workspace.root_path / "alpha" / "r1"


def test_get_feature_worktree_pairs_env_and_repo(workspace: Workspace, service: WorkspaceService) -> None:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    repo = ProjectRepository(name="demo", main_path=workspace.root_path / "projects" / "demo", main_branch="main")
    wt = service.get_feature_worktree(env, repo)
    assert isinstance(wt, FeatureWorktree)
    assert wt.repository.name == "demo"
    assert wt.environment.name == "alpha"


def test_connect_env_sets_upstream_for_non_pinned(
    workspace: Workspace, service: WorkspaceService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """`connect_env` invokes `set_upstream(origin/<feature>) + set_push_default` per non-pinned worktree."""
    repos = [
        ProjectRepository(name="feature-repo", main_path=workspace.root_path / "feature-repo", main_branch="main"),
        ProjectRepository(
            name="pinned-repo", main_path=workspace.root_path / "pinned-repo", main_branch="main", pinned=True
        ),
    ]
    env_wts = _env_worktrees(workspace, repos)

    count = service.connect_env(env_wts, feature_branch="feature/widget")

    assert count == 1
    assert fake_repo_repo.set_upstream_calls == [("feature-repo", "origin/feature/widget")]
    assert fake_repo_repo.set_push_default_calls == ["feature-repo"]


def test_disconnect_env_skips_pinned_and_unsets_others(
    workspace: Workspace, service: WorkspaceService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    repos = [
        ProjectRepository(name="feature-repo", main_path=workspace.root_path / "feature-repo", main_branch="main"),
        ProjectRepository(
            name="pinned-repo", main_path=workspace.root_path / "pinned-repo", main_branch="main", pinned=True
        ),
    ]
    env_wts = _env_worktrees(workspace, repos)

    count = service.disconnect_env(env_wts)

    assert count == 1
    assert fake_repo_repo.unset_upstream_calls == ["feature-repo"]


def test_get_environment_status_delegates_to_worktree_repo(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
) -> None:
    """The service returns the status verbatim when no env decorators are supplied."""
    fake_worktree_repo = FakeReadWorkspaceRepository(feature_branch="feature/x")
    fake_repo_repo = FakeWriteRepoRepository()
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    svc = WorkspaceService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")

    status = svc.get_environment_status(env, project_repos=[])
    assert status.feature_branch == "feature/x"
