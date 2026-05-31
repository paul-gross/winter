from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    RepoError,
    RepoStatus,
    Workspace,
    WorktreeRepoStatus,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


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
    """Stub for the `IWriteRepoRepository` Protocol — raises on any unexpected call."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


def _service(feature_branch: str | None = None) -> EnvStatusService:
    return EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(feature_branch=feature_branch),  # type: ignore[arg-type]
        repo_repo=FakeWriteRepoRepository(),  # type: ignore[arg-type]
    )


def test_get_feature_environment_worktrees_builds_one_per_repo(workspace: Workspace) -> None:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    repos = [
        ProjectRepository(name="r1", main_path=workspace.root_path / "projects" / "r1", main_branch="main"),
        ProjectRepository(name="r2", main_path=workspace.root_path / "projects" / "r2", main_branch="main"),
    ]

    env_wts = _service().get_feature_environment_worktrees(env, repos)

    assert env_wts.environment is env
    assert [wt.repository.name for wt in env_wts.worktrees] == ["r1", "r2"]
    assert env_wts.worktrees[0].path == workspace.root_path / "alpha" / "r1"


def test_get_feature_worktree_pairs_env_and_repo(workspace: Workspace) -> None:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    repo = ProjectRepository(name="demo", main_path=workspace.root_path / "projects" / "demo", main_branch="main")
    wt = _service().get_feature_worktree(env, repo)
    assert isinstance(wt, FeatureWorktree)
    assert wt.repository.name == "demo"
    assert wt.environment.name == "alpha"


def test_get_environment_status_delegates_to_worktree_repo(workspace: Workspace) -> None:
    """The service returns the status verbatim when no env decorators are supplied."""
    svc = _service(feature_branch="feature/x")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")

    status = svc.get_environment_status(env, project_repos=[])
    assert status.feature_branch == "feature/x"


# ── get_worktree_repo_statuses (parallel fan-out) ────────────────────────────


class StatusByRepoRepository:
    """`IWriteRepoRepository` stub that answers `get_worktree_status` per repo.

    Maps repo name → a `RepoStatus` to return, or a `RepoError` to raise. Any
    other attribute access fails loudly so unexpected calls surface.
    """

    def __init__(
        self,
        statuses: dict[str, RepoStatus] | None = None,
        errors: dict[str, RepoError] | None = None,
        on_call: Any = None,
    ) -> None:
        self._statuses = statuses or {}
        self._errors = errors or {}
        self._on_call = on_call

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        name = worktree.repository.name
        if self._on_call is not None:
            self._on_call(name)
        if name in self._errors:
            raise self._errors[name]
        return self._statuses[name]

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"StatusByRepoRepository.{name} called unexpectedly")


def _env_worktrees(workspace: Workspace, repo_names: list[str]) -> FeatureEnvironmentWorktrees:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    repos = [
        ProjectRepository(name=n, main_path=workspace.root_path / "projects" / n, main_branch="main")
        for n in repo_names
    ]
    return EnvStatusService(  # reuse the service's builder so paths/topology match prod
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=FakeWriteRepoRepository(),  # type: ignore[arg-type]
    ).get_feature_environment_worktrees(env, repos)


def _status(name: str, *, ahead: int = 0, dirty: int = 0) -> RepoStatus:
    return RepoStatus(
        name=name,
        path=f"/ws/alpha/{name}",
        main_branch="main",
        branch="alpha",
        ahead=ahead,
        behind=0,
        dirty_files=[f"f{i}.txt" for i in range(dirty)],
        tracking_branch="origin/feature",
        tracking_ahead=ahead,
        tracking_behind=0,
        tracking_ref_present=True,
    )


def test_get_worktree_repo_statuses_preserves_worktree_order(workspace: Workspace) -> None:
    """Even though reads fan out across threads, results come back in worktree order."""
    names = ["r1", "r2", "r3", "r4", "r5"]
    env_wts = _env_worktrees(workspace, names)
    repo = StatusByRepoRepository(statuses={n: _status(n, ahead=i, dirty=i) for i, n in enumerate(names)})

    rows: list[WorktreeRepoStatus] = EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=repo,  # type: ignore[arg-type]
    ).get_worktree_repo_statuses(env_wts)

    assert [r.worktree.repository.name for r in rows] == names
    # Fields are mapped straight through from RepoStatus → WorktreeRepoStatus.
    assert [r.ahead for r in rows] == [0, 1, 2, 3, 4]
    assert [r.dirty_count for r in rows] == [0, 1, 2, 3, 4]
    assert all(r.tracking_ref_present for r in rows)


def test_get_worktree_repo_statuses_empty_env_returns_empty(workspace: Workspace) -> None:
    env_wts = _env_worktrees(workspace, [])
    rows = EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=StatusByRepoRepository(),  # type: ignore[arg-type]
    ).get_worktree_repo_statuses(env_wts)
    assert rows == []


def test_get_worktree_repo_statuses_propagates_repo_error_without_callback(workspace: Workspace) -> None:
    """With no on_repo_error callback the failing worktree's RepoError propagates."""
    names = ["r1", "r2", "r3"]
    env_wts = _env_worktrees(workspace, names)
    repo = StatusByRepoRepository(
        statuses={"r1": _status("r1"), "r3": _status("r3")},
        errors={"r2": RepoError("boom on r2")},
    )
    svc = EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=repo,  # type: ignore[arg-type]
    )
    with pytest.raises(RepoError, match="boom on r2"):
        svc.get_worktree_repo_statuses(env_wts)


def test_get_worktree_repo_statuses_reports_and_skips_with_callback(workspace: Workspace) -> None:
    """With an on_repo_error callback the failing worktree is reported and skipped."""
    names = ["r1", "r2", "r3"]
    env_wts = _env_worktrees(workspace, names)
    repo = StatusByRepoRepository(
        statuses={"r1": _status("r1"), "r3": _status("r3")},
        errors={"r2": RepoError("boom on r2")},
    )
    reported: list[tuple[str, str]] = []

    rows = EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=repo,  # type: ignore[arg-type]
    ).get_worktree_repo_statuses(
        env_wts,
        on_repo_error=lambda wt, exc: reported.append((wt.repository.name, str(exc))),
    )

    assert [r.worktree.repository.name for r in rows] == ["r1", "r3"]
    assert [name for name, _ in reported] == ["r2"]


def test_get_worktree_repo_statuses_runs_concurrently(workspace: Workspace) -> None:
    """The reads overlap: a barrier sized to the repo count only releases if
    every worktree's read is in flight at once — a serial loop would deadlock."""
    import threading

    names = ["r1", "r2", "r3", "r4"]
    env_wts = _env_worktrees(workspace, names)
    barrier = threading.Barrier(len(names), timeout=5)

    def rendezvous(_name: str) -> None:
        barrier.wait()  # blocks until all N reads have arrived — proves overlap

    repo = StatusByRepoRepository(
        statuses={n: _status(n) for n in names},
        on_call=rendezvous,
    )

    rows = EnvStatusService(
        worktree_repo=FakeReadWorkspaceRepository(),  # type: ignore[arg-type]
        repo_repo=repo,  # type: ignore[arg-type]
    ).get_worktree_repo_statuses(env_wts)

    assert [r.worktree.repository.name for r in rows] == names
