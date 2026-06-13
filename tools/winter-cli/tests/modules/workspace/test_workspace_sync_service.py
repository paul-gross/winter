from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.fetch_reporter import IFetchReporter
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    PullMode,
    RepoError,
    RepoScope,
    RepoSyncOutcome,
    SyncResult,
    Workspace,
)
from winter_cli.modules.workspace.pull_reporter import IPullReporter
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService

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
    def get_environments(
        self, workspace: Workspace, project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        return []

    def get_environment_status(
        self, env: FeatureEnvironment, project_repos: list[ProjectRepository]
    ) -> FeatureEnvironmentStatus:
        return FeatureEnvironmentStatus(environment=env, feature_branch=None)


class FakeWriteRepoRepository:
    """No-op repo for empty-input smoke tests.

    `sync_ff_only` exists as a no-op rather than raising on `__getattr__`
    because `fetch_all` fetches + fast-forwards each matched source repo
    through it. The empty-input tests below never reach that fan-out, but
    keeping it a no-op (instead of letting `__getattr__` raise) documents the
    accessor `fetch_all` uses. Other attribute accesses still raise so
    accidental fan-out trips the test.
    """

    def sync_ff_only(self, repo: ProjectRepository) -> int:
        return 0

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


class _NullFetchReporter:
    def fetch_started(self) -> None:
        return None

    def repo_fetched(self, scope: str, repo: str, success: bool, commits: int, error: str | None) -> None:
        return None

    def fetch_completed(self, success: bool) -> None:
        return None


class _NullPullReporter:
    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def repo_synced(self, scope: str, repo: str, result: Any, commits: int, ahead: int, behind: int) -> None:
        return None

    def pull_completed(self, success: bool) -> None:
        return None


def _make_service(workspace: Workspace, workspace_config: WorkspaceConfig) -> WorkspaceSyncService:
    fake_worktree_repo = FakeReadWorkspaceRepository()
    fake_repo_repo = FakeWriteRepoRepository()
    env_status_svc = EnvStatusService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
    )
    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    return WorkspaceSyncService(
        env_status_svc=env_status_svc,
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )


def test_construct_sync_service(workspace: Workspace, workspace_config: WorkspaceConfig) -> None:
    """Smoke test: WorkspaceSyncService can be assembled from its dependencies.

    The substantive sync/fetch/pull behaviour is exercised via integration in
    the dashboard; this unit-level test just locks the constructor signature
    so DI rewiring fails loudly.
    """
    fake_worktree_repo = FakeReadWorkspaceRepository()
    fake_repo_repo = FakeWriteRepoRepository()
    env_status_svc = EnvStatusService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
    )
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)

    svc = WorkspaceSyncService(
        env_status_svc=env_status_svc,
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=fake_repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )
    assert isinstance(svc, WorkspaceSyncService)


def test_get_feature_environment_worktrees_helper_unused_directly(workspace: Workspace) -> None:
    """FeatureWorktree construction is owned by EnvStatusService now; this test pins that contract."""
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    wt = FeatureWorktree(
        workspace=workspace,
        environment=env,
        repository=ProjectRepository(name="demo", main_path=workspace.root_path / "demo", main_branch="main"),
    )
    assert wt.repository.name == "demo"


def test_fetch_all_with_no_envs_or_standalones_returns_empty_report(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """No envs (FakeReadWorkspaceRepository returns []) and project-only scope → empty report, no reporter calls."""
    svc = _make_service(workspace, workspace_config)
    reporter: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    assert report.projects == []
    assert report.standalone == []
    assert report.success is True


def test_pull_all_with_no_envs_or_standalones_returns_empty_report(
    workspace: Workspace, workspace_config: WorkspaceConfig
) -> None:
    """Same shape as fetch_all: empty inputs → empty report, no integrate happens."""
    svc = _make_service(workspace, workspace_config)
    reporter: IPullReporter = _NullPullReporter()  # type: ignore[assignment]

    report = svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,
    )

    assert report.envs == []
    assert report.standalone == []
    assert report.skipped == []
    assert report.success is True


class _SpyWriteRepoRepository:
    """Records `sync_ff_only` calls and fails loudly if `fetch` is used.

    Pins `fetch_all`'s contract: it refreshes + fast-forwards each project repo
    through `sync_ff_only` against the source checkout, never a per-worktree
    `fetch`. `raise_on` names a repo whose `sync_ff_only` raises, modelling a
    diverged source main.
    """

    def __init__(self, raise_on: str | None = None, commits: int = 0) -> None:
        self.synced: list[ProjectRepository] = []
        self._raise_on = raise_on
        self._commits = commits
        self._lock = threading.Lock()

    def sync_ff_only(self, repo: ProjectRepository) -> int:
        with self._lock:
            self.synced.append(repo)
        if repo.name == self._raise_on:
            raise RepoError(f"sync_ff_only failed for {repo.name}", cwd=str(repo.main_path))
        return self._commits

    def fetch(self, worktree: FeatureWorktree) -> None:
        raise AssertionError("fetch_all must fast-forward via sync_ff_only, not fetch")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_SpyWriteRepoRepository.{name} called unexpectedly")


class _FakeEnvStatusService:
    """Returns a fixed worktree set, sidestepping on-disk worktree discovery."""

    def __init__(self, env_worktrees: FeatureEnvironmentWorktrees) -> None:
        self._env_worktrees = env_worktrees

    def get_feature_environment_worktrees(
        self, env: FeatureEnvironment, project_repos: list[ProjectRepository]
    ) -> FeatureEnvironmentWorktrees:
        return self._env_worktrees


def _make_fetch_service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    env_worktrees: FeatureEnvironmentWorktrees,
    repo_repo: _SpyWriteRepoRepository,
) -> WorkspaceSyncService:
    class _OneEnvWorktreeRepo(FakeReadWorkspaceRepository):
        def get_environments(self, workspace_, project_repos):  # type: ignore[no-untyped-def]
            return [env_worktrees.environment]

    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    return WorkspaceSyncService(
        env_status_svc=_FakeEnvStatusService(env_worktrees),  # type: ignore[arg-type]
        worktree_repo=_OneEnvWorktreeRepo(),  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )


def _make_env_with_worktree(
    workspace: Workspace, tmp_path: Path
) -> tuple[FeatureEnvironmentWorktrees, ProjectRepository]:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=tmp_path / "alpha")
    repo = ProjectRepository(name="demo", main_path=tmp_path / "projects" / "demo", main_branch="main")
    wt = FeatureWorktree(workspace=workspace, environment=env, repository=repo)
    wt.path.mkdir(parents=True)  # _warn_unless_present drops worktrees missing on disk
    return FeatureEnvironmentWorktrees(environment=env, worktrees=[wt]), repo


def test_fetch_all_fast_forwards_source_checkouts_via_sync_ff_only(
    workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """fetch_all routes each matched project repo through sync_ff_only (not fetch).

    sync_ff_only fetches the shared source-checkout `.git` and fast-forwards
    its local main; doing it here is what keeps `winter ws init`'s branch base
    current.
    """
    workspace = Workspace(root_path=tmp_path, session_prefix="t", main_branch="main")
    env_worktrees, repo = _make_env_with_worktree(workspace, tmp_path)
    repo_repo = _SpyWriteRepoRepository()
    svc = _make_fetch_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    assert [r.name for r in repo_repo.synced] == ["demo"]
    assert repo_repo.synced[0].main_path == repo.main_path
    assert [o.repo_name for o in report.projects] == ["demo"]
    assert report.success is True


def test_fetch_all_propagates_sync_ff_only_commit_count(workspace_config: WorkspaceConfig, tmp_path: Path) -> None:
    """The commit count `sync_ff_only` returns surfaces on the per-repo outcome."""
    workspace = Workspace(root_path=tmp_path, session_prefix="t", main_branch="main")
    env_worktrees, _ = _make_env_with_worktree(workspace, tmp_path)
    repo_repo = _SpyWriteRepoRepository(commits=4)
    svc = _make_fetch_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    assert [o.commits for o in report.projects] == [4]
    assert report.success is True


def test_fetch_all_reports_failure_when_source_checkout_diverges(
    workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """A RepoError from sync_ff_only (e.g. diverged source main) is a failed fetch."""
    workspace = Workspace(root_path=tmp_path, session_prefix="t", main_branch="main")
    env_worktrees, _ = _make_env_with_worktree(workspace, tmp_path)
    repo_repo = _SpyWriteRepoRepository(raise_on="demo")
    svc = _make_fetch_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    assert len(report.projects) == 1
    outcome = report.projects[0]
    assert outcome.repo_name == "demo"
    assert outcome.success is False
    assert outcome.error is not None
    assert report.success is False


class _PullSpyWriteRepoRepository:
    """Fake write repo for `pull_all`: per-worktree upstreams + recorded integrates.

    `upstreams` maps repo name → the worktree's own tracking branch (e.g.
    `origin/featbranch`), or `None` for a worktree with no upstream. `integrate`
    is only meant to be reached for worktrees that resolved a ref; it records
    `(repo_name, target_ref)` so a test can pin *which* ref each worktree pulled
    from — proving per-worktree resolution rather than one env-wide ref.
    """

    def __init__(
        self,
        upstreams: dict[str, str | None],
        integrate_results: dict[str, SyncResult] | None = None,
    ) -> None:
        self._upstreams = upstreams
        self._integrate_results = integrate_results or {}
        self.fetched: list[str] = []
        self.integrated: list[tuple[str, str]] = []
        self.upstream_queries: list[str] = []
        self._lock = threading.Lock()

    def fetch(self, worktree: FeatureWorktree) -> None:
        with self._lock:
            self.fetched.append(worktree.repository.name)

    def get_worktree_upstream(self, worktree: FeatureWorktree) -> str | None:
        with self._lock:
            self.upstream_queries.append(worktree.repository.name)
        return self._upstreams.get(worktree.repository.name)

    def integrate(self, worktree: FeatureWorktree, target_ref: str, mode: PullMode, autostash: bool) -> RepoSyncOutcome:
        name = worktree.repository.name
        with self._lock:
            self.integrated.append((name, target_ref))
        result = self._integrate_results.get(name, SyncResult.fast_forwarded)
        commits = 1 if result == SyncResult.fast_forwarded else 0
        return RepoSyncOutcome(repo_name=name, sync_result=result, commits=commits)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_PullSpyWriteRepoRepository.{name} called unexpectedly")


class _RecordingPullReporter:
    """Captures `repo_synced` events so a test can assert per-worktree outcomes."""

    def __init__(self) -> None:
        self.synced: list[tuple[str, str, SyncResult]] = []
        self.completed_success: bool | None = None

    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def repo_synced(self, scope: str, repo: str, result: Any, commits: int, ahead: int, behind: int) -> None:
        self.synced.append((scope, repo, result))

    def pull_completed(self, success: bool) -> None:
        self.completed_success = success


def _make_pull_service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    env_worktrees: FeatureEnvironmentWorktrees,
    repo_repo: _PullSpyWriteRepoRepository,
) -> WorkspaceSyncService:
    class _OneEnvWorktreeRepo(FakeReadWorkspaceRepository):
        def get_environments(self, workspace_, project_repos):  # type: ignore[no-untyped-def]
            return [env_worktrees.environment]

    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    return WorkspaceSyncService(
        env_status_svc=_FakeEnvStatusService(env_worktrees),  # type: ignore[arg-type]
        worktree_repo=_OneEnvWorktreeRepo(),  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
    )


def _make_env_with_named_worktrees(
    workspace: Workspace, tmp_path: Path, repo_names: list[str]
) -> FeatureEnvironmentWorktrees:
    """Build a one-env worktree set with several non-pinned repos, each on disk."""
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=tmp_path / "alpha")
    worktrees: list[FeatureWorktree] = []
    for name in repo_names:
        repo = ProjectRepository(name=name, main_path=tmp_path / "projects" / name, main_branch="main")
        wt = FeatureWorktree(workspace=workspace, environment=env, repository=repo)
        wt.path.mkdir(parents=True)  # _warn_unless_present drops worktrees missing on disk
        worktrees.append(wt)
    return FeatureEnvironmentWorktrees(environment=env, worktrees=worktrees)


def test_pull_all_mixed_env_skips_no_upstream_worktree_and_ffs_connected_one(
    workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """Mixed env: one worktree tracks a feature branch, the other has no upstream.

    The connected worktree fast-forwards from its own ref; the no-upstream
    worktree yields `no_upstream` (never reaches integrate); the env report
    still succeeds (no_upstream is excluded from the success calc).
    """
    workspace = Workspace(root_path=tmp_path, session_prefix="t", main_branch="main")
    env_worktrees = _make_env_with_named_worktrees(workspace, tmp_path, ["repo-a", "repo-b"])
    repo_repo = _PullSpyWriteRepoRepository(upstreams={"repo-a": "origin/featbranch", "repo-b": None})
    svc = _make_pull_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter = _RecordingPullReporter()

    report = svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # repo-a pulled from its own upstream; repo-b never integrated.
    assert repo_repo.integrated == [("repo-a", "origin/featbranch")]
    outcomes = {o.repo_name: o.sync_result for o in report.envs[0].repos}
    assert outcomes == {"repo-a": SyncResult.fast_forwarded, "repo-b": SyncResult.no_upstream}
    assert report.envs[0].success is True
    assert report.success is True
    # The per-worktree no_upstream outcome is reported (drives stream + --json).
    assert ("alpha", "repo-b", SyncResult.no_upstream) in reporter.synced


def test_pull_all_resolves_upstream_per_worktree_regardless_of_repo_order(
    workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """A connected worktree pulls from its own upstream even when the *first*
    repo has none — no demotion to `origin/<main_branch>`, no env-wide ref."""
    workspace = Workspace(root_path=tmp_path, session_prefix="t", main_branch="main")
    # repo-a (first) has NO upstream; repo-b (later) is the connected one.
    env_worktrees = _make_env_with_named_worktrees(workspace, tmp_path, ["repo-a", "repo-b"])
    repo_repo = _PullSpyWriteRepoRepository(upstreams={"repo-a": None, "repo-b": "origin/other-feat"})
    svc = _make_pull_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter = _RecordingPullReporter()

    report = svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # repo-b pulled from ITS OWN ref, not origin/main and not repo-a's (absent) ref.
    assert repo_repo.integrated == [("repo-b", "origin/other-feat")]
    outcomes = {o.repo_name: o.sync_result for o in report.envs[0].repos}
    assert outcomes == {"repo-a": SyncResult.no_upstream, "repo-b": SyncResult.fast_forwarded}
    assert report.success is True
