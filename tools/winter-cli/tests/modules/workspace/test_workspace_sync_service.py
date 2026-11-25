from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeConfigLockRepository, FakeGitRepository
from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    StandaloneRepositoryConfig,
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
    SourceCheckoutSync,
    StandaloneRepository,
    SyncResult,
    Workspace,
)
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
from winter_cli.modules.workspace.pull_reporter import IPullReporter
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, service_prefix="t", main_branch="main")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
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

    def sync_ff_only(self, repo: ProjectRepository) -> SourceCheckoutSync:
        return SourceCheckoutSync(main_branch=repo.main_branch, current_branch=repo.main_branch)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


class _NullFetchReporter:
    def fetch_started(self) -> None:
        return None

    def repo_fetched(
        self,
        scope_label: str,
        repo_name: str,
        success: bool,
        commits: int,
        error: str | None,
        warning: str | None = None,
    ) -> None:
        return None

    def fetch_completed(self, success: bool) -> None:
        return None


class _RecordingFetchReporter(_NullFetchReporter):
    """Captures `repo_fetched` warnings so a test can assert the reporter receives them."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, str | None]] = []

    def repo_fetched(
        self,
        scope_label: str,
        repo_name: str,
        success: bool,
        commits: int,
        error: str | None,
        warning: str | None = None,
    ) -> None:
        self.warnings.append((repo_name, warning))


class _NullPullReporter:
    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def source_checkout_warning(self, repo_name: str, warning: str) -> None:
        return None

    def repo_synced(
        self, scope: str, repo: str, result: Any, commits: int, ahead: int, behind: int, pin_ref: str = ""
    ) -> None:
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

    def __init__(self, raise_on: str | None = None, commits: int = 0, current_branch: str | None = "main") -> None:
        self.synced: list[ProjectRepository] = []
        self._raise_on = raise_on
        self._commits = commits
        self._current_branch = current_branch
        self._lock = threading.Lock()

    def sync_ff_only(self, repo: ProjectRepository) -> SourceCheckoutSync:
        with self._lock:
            self.synced.append(repo)
        if repo.name == self._raise_on:
            raise RepoError(f"sync_ff_only failed for {repo.name}", cwd=str(repo.main_path))
        return SourceCheckoutSync(
            main_branch=repo.main_branch, current_branch=self._current_branch, commits=self._commits
        )

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
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
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
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
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
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
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


@pytest.mark.parametrize(
    ("current_branch", "expected_fragment"),
    [("master", "branch 'master'"), (None, "a detached HEAD")],
)
def test_fetch_all_warns_when_source_checkout_is_off_main_branch(
    workspace_config: WorkspaceConfig, tmp_path: Path, current_branch: str | None, expected_fragment: str
) -> None:
    """An off-main source checkout is a successful fetch whose warning reaches the reporter."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    env_worktrees, _ = _make_env_with_worktree(workspace, tmp_path)
    repo_repo = _SpyWriteRepoRepository(current_branch=current_branch)
    svc = _make_fetch_service(workspace, workspace_config, env_worktrees, repo_repo)
    recorder = _RecordingFetchReporter()
    reporter: IFetchReporter = recorder  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    outcome = report.projects[0]
    assert outcome.success is True
    assert outcome.commits == 0
    assert outcome.warning is not None
    assert expected_fragment in outcome.warning
    assert "main_branch 'main'" in outcome.warning
    assert recorder.warnings == [(outcome.repo_name, outcome.warning)]


def test_fetch_all_on_main_branch_has_no_warning(workspace_config: WorkspaceConfig, tmp_path: Path) -> None:
    """A source checkout on its main branch fetches without a warning."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    env_worktrees, _ = _make_env_with_worktree(workspace, tmp_path)
    repo_repo = _SpyWriteRepoRepository(commits=2)
    svc = _make_fetch_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]

    report = svc.fetch_all(scope=RepoScope.project, patterns=None, reporter=reporter)

    assert report.projects[0].warning is None
    assert report.projects[0].commits == 2


class _PullSpyWriteRepoRepository:
    """Fake write repo for `pull_all`: per-worktree upstreams + recorded integrates.

    `upstreams` maps repo name → the worktree's own tracking branch (e.g.
    `origin/featbranch`), or `None` for a worktree with no upstream. `integrate`
    is only meant to be reached for worktrees that resolved a ref; it records
    `(repo_name, target_ref)` so a test can pin *which* ref each worktree pulled
    from — proving per-worktree resolution rather than one env-wide ref.
    `raise_sync_ff_only` names a repo whose `sync_ff_only` raises, modelling a
    diverged or non-ff-able source checkout.
    """

    def __init__(
        self,
        upstreams: dict[str, str | None],
        integrate_results: dict[str, SyncResult] | None = None,
        raise_sync_ff_only: str | None = None,
        source_branch: str | None = "main",
    ) -> None:
        self._upstreams = upstreams
        self._integrate_results = integrate_results or {}
        self._raise_sync_ff_only = raise_sync_ff_only
        self._source_branch = source_branch
        self.synced_ff: list[str] = []
        self.integrated: list[tuple[str, str]] = []
        self.upstream_queries: list[str] = []
        self._lock = threading.Lock()

    def sync_ff_only(self, repo: ProjectRepository) -> SourceCheckoutSync:
        with self._lock:
            self.synced_ff.append(repo.name)
        if repo.name == self._raise_sync_ff_only:
            raise RepoError(f"sync_ff_only failed for {repo.name}", cwd=str(repo.main_path))
        return SourceCheckoutSync(main_branch=repo.main_branch, current_branch=self._source_branch)

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
        self.source_warnings: list[tuple[str, str]] = []
        self.completed_success: bool | None = None

    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def source_checkout_warning(self, repo_name: str, warning: str) -> None:
        self.source_warnings.append((repo_name, warning))

    def repo_synced(
        self, scope: str, repo: str, result: Any, commits: int, ahead: int, behind: int, pin_ref: str = ""
    ) -> None:
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
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
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

    # source checkout sync ran for both groups (each repo is its own group).
    assert set(repo_repo.synced_ff) == {"repo-a", "repo-b"}
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
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
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

    # source checkout sync ran for both groups (each repo is its own group).
    assert set(repo_repo.synced_ff) == {"repo-a", "repo-b"}
    # repo-b pulled from ITS OWN ref, not origin/main and not repo-a's (absent) ref.
    assert repo_repo.integrated == [("repo-b", "origin/other-feat")]
    outcomes = {o.repo_name: o.sync_result for o in report.envs[0].repos}
    assert outcomes == {"repo-a": SyncResult.no_upstream, "repo-b": SyncResult.fast_forwarded}
    assert report.success is True


def test_pull_all_source_checkout_sync_failure_is_best_effort(
    workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """A RepoError from sync_ff_only (e.g. diverged source main) does not fail the pull.

    The worktree integrates still run and succeed; the overall report is success.
    The source-checkout sync is best-effort: a non-ff-able checkout logs a warning
    and pull continues.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    env_worktrees = _make_env_with_named_worktrees(workspace, tmp_path, ["repo-a"])
    repo_repo = _PullSpyWriteRepoRepository(
        upstreams={"repo-a": "origin/featbranch"},
        raise_sync_ff_only="repo-a",
    )
    svc = _make_pull_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter = _RecordingPullReporter()

    report = svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # sync_ff_only was attempted (and raised), but integrate still ran.
    assert repo_repo.synced_ff == ["repo-a"]
    assert repo_repo.integrated == [("repo-a", "origin/featbranch")]
    assert report.envs[0].success is True
    assert report.success is True


# ── Phase 5: pin-aware standalone pull ────────────────────────────────────────

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


class _StandaloneSpyWriteRepoRepository:
    """Minimal fake for standalone pull tests.

    Records ``fetch_standalone``, ``integrate_standalone``, and
    ``integrate_standalone_to_ref`` calls. Tests that exercise the pinned
    branch-ff path should see no ``integrate_standalone`` call — the service
    calls ``integrate_standalone_to_ref`` instead for branch pins.
    """

    def __init__(
        self,
        integrate_result: SyncResult = SyncResult.up_to_date,
        integrate_to_ref_result: SyncResult = SyncResult.fast_forwarded,
    ) -> None:
        self.fetched_standalones: list[str] = []
        self.integrated_standalones: list[str] = []
        self.integrated_to_ref: list[tuple[str, str]] = []
        self._integrate_result = integrate_result
        self._integrate_to_ref_result = integrate_to_ref_result

    def fetch_standalone(self, repo: StandaloneRepository) -> None:
        self.fetched_standalones.append(repo.name)

    def integrate_standalone(
        self,
        repo: StandaloneRepository,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        self.integrated_standalones.append(repo.name)
        return RepoSyncOutcome(repo_name=repo.name, sync_result=self._integrate_result)

    def integrate_standalone_to_ref(
        self,
        repo: StandaloneRepository,
        target_ref: str,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        self.integrated_to_ref.append((repo.name, target_ref))
        return RepoSyncOutcome(repo_name=repo.name, sync_result=self._integrate_to_ref_result)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_StandaloneSpyWriteRepoRepository.{name} called unexpectedly")


def _make_standalone_workspace_config(
    workspace_root: Path,
    standalone_name: str,
    ref: str | None,
) -> WorkspaceConfig:
    """Build a WorkspaceConfig with a single standalone repo (no project repos)."""
    return WorkspaceConfig(
        workspace_root=workspace_root,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        standalone_repos=[
            StandaloneRepositoryConfig(name=standalone_name, ref=ref),
        ],
    )


def _make_standalone_service(
    workspace: Workspace,
    workspace_config: WorkspaceConfig,
    repo_repo: _StandaloneSpyWriteRepoRepository,
    git_repo: FakeGitRepository | None = None,
    config_lock_repo: FakeConfigLockRepository | None = None,
) -> WorkspaceSyncService:
    fake_worktree_repo = FakeReadWorkspaceRepository()
    env_status_svc = EnvStatusService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
    )
    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    return WorkspaceSyncService(
        env_status_svc=env_status_svc,
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(workspace_config),
        workspace=workspace,
        git_ops=git_ops,
        git_repo=git_repo,  # type: ignore[arg-type]
        config_lock_repo=config_lock_repo,  # type: ignore[arg-type]
    )


def test_standalone_branch_ref_advances_working_tree_and_rewrites_lock(tmp_path: Path) -> None:
    """branch ref + origin advanced → working tree fast-forwards via ff-only, lock rewritten.

    Moving-pin path: after fetch, resolve_ref classifies repo.ref as a branch;
    the service calls integrate_standalone_to_ref (ff-only, refuses on divergence)
    rather than checkout_branch (which would force-reset). When the integration
    returns fast_forwarded, get_head_commit reads the new SHA, the lock is rewritten,
    and the outcome is re_pinned with the short SHA as pin_ref.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref="main")

    git_repo = FakeGitRepository()
    git_repo.resolved_refs[(repo_path, "main")] = (RefKind.branch, SHA_NEW)
    # Mark as clean so the dirty guard passes.
    git_repo.clean_worktrees.add(repo_path)
    # After ff integrate, HEAD is the new SHA.
    git_repo.head_commits[repo_path] = SHA_NEW

    lock_repo = FakeConfigLockRepository()
    # Spy: integrate_standalone_to_ref returns fast_forwarded (default).
    repo_repo = _StandaloneSpyWriteRepoRepository(integrate_to_ref_result=SyncResult.fast_forwarded)
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    report = svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # integrate_standalone must NOT have been called — pin-aware path bypasses it.
    assert repo_repo.integrated_standalones == []
    # integrate_standalone_to_ref was called with origin/<ref>.
    assert ("my-lib", "origin/main") in repo_repo.integrated_to_ref
    # checkout_branch must NOT have been called (ff-only refuses divergence; no force-reset).
    assert git_repo.branch_checkouts == []
    # Lock was rewritten with the new SHA.
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    assert "my-lib" in written
    assert written["my-lib"].commit == SHA_NEW
    assert written["my-lib"].kind is RefKind.branch
    # Outcome is re_pinned with short SHA as pin_ref.
    assert report.standalone[0].sync_result == SyncResult.re_pinned
    assert report.standalone[0].pin_ref == SHA_NEW[:8]
    assert ("standalone", "my-lib", SyncResult.re_pinned) in reporter.synced
    # A standalone is not a feature worktree: every git call it makes names no env.
    assert git_repo.env_calls
    assert {env for _method, _path, env in git_repo.env_calls} == {None}
    assert {repo_name for _method, _path, repo_name in git_repo.repo_name_calls} == {"my-lib"}


def test_standalone_branch_ref_up_to_date_does_not_rewrite_lock(tmp_path: Path) -> None:
    """branch ref already up to date → integrate_standalone_to_ref returns up_to_date, no lock rewrite."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref="main")

    git_repo = FakeGitRepository()
    git_repo.resolved_refs[(repo_path, "main")] = (RefKind.branch, SHA_OLD)
    git_repo.clean_worktrees.add(repo_path)
    git_repo.head_commits[repo_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    # Spy: integrate_standalone_to_ref returns up_to_date (HEAD already at origin tip).
    repo_repo = _StandaloneSpyWriteRepoRepository(integrate_to_ref_result=SyncResult.up_to_date)
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    report = svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # integrate_standalone_to_ref was called.
    assert ("my-lib", "origin/main") in repo_repo.integrated_to_ref
    # Lock must NOT have been rewritten — no fast_forwarded outcome.
    assert lock_repo.write_calls == []
    assert report.standalone[0].sync_result == SyncResult.up_to_date
    assert ("standalone", "my-lib", SyncResult.up_to_date) in reporter.synced


def test_standalone_tag_ref_held_no_integrate_no_lock_change(tmp_path: Path) -> None:
    """tag ref → HELD: no checkout, no lock rewrite, outcome held_pin with the tag as pin_ref."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref="v1.4.2")

    existing_entry = LockEntry(name="my-lib", ref="v1.4.2", kind=RefKind.tag, commit=SHA_OLD)
    lock_repo = FakeConfigLockRepository(entries={"my-lib": existing_entry})

    git_repo = FakeGitRepository()
    git_repo.resolved_refs[(repo_path, "v1.4.2")] = (RefKind.tag, SHA_OLD)
    git_repo.head_commits[repo_path] = SHA_OLD

    repo_repo = _StandaloneSpyWriteRepoRepository()
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    report = svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # No checkout called at all.
    assert git_repo.detached_checkouts == []
    assert git_repo.branch_checkouts == []
    # No integrate_standalone called.
    assert repo_repo.integrated_standalones == []
    # Lock unchanged.
    assert lock_repo.write_calls == []
    # Outcome is held_pin with the tag ref as pin_ref.
    assert report.standalone[0].sync_result == SyncResult.held_pin
    assert report.standalone[0].pin_ref == "v1.4.2"
    assert ("standalone", "my-lib", SyncResult.held_pin) in reporter.synced


def test_standalone_commit_ref_held_no_integrate_no_lock_change(tmp_path: Path) -> None:
    """commit SHA ref → HELD likewise: no checkout, no lock rewrite, outcome held_pin."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref=SHA_OLD)

    git_repo = FakeGitRepository()
    git_repo.resolved_refs[(repo_path, SHA_OLD)] = (RefKind.commit, SHA_OLD)
    git_repo.head_commits[repo_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = _StandaloneSpyWriteRepoRepository()
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    report = svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    assert git_repo.detached_checkouts == []
    assert git_repo.branch_checkouts == []
    assert repo_repo.integrated_standalones == []
    assert lock_repo.write_calls == []
    assert report.standalone[0].sync_result == SyncResult.held_pin
    assert report.standalone[0].pin_ref == SHA_OLD


def test_standalone_no_ref_uses_existing_integrate_path(tmp_path: Path) -> None:
    """ref is None → existing integrate_standalone behavior, no pin logic invoked."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref=None)

    git_repo = FakeGitRepository()
    lock_repo = FakeConfigLockRepository()
    repo_repo = _StandaloneSpyWriteRepoRepository(integrate_result=SyncResult.fast_forwarded)
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    report = svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    # integrate_standalone must have been called (unpinned path).
    assert repo_repo.integrated_standalones == ["my-lib"]
    # No resolve_ref, no checkout, no lock write.
    assert git_repo.branch_checkouts == []
    assert git_repo.detached_checkouts == []
    assert lock_repo.write_calls == []
    assert report.standalone[0].sync_result == SyncResult.fast_forwarded


def test_fetch_standalone_tag_pin_does_not_change_head_or_lock(tmp_path: Path) -> None:
    """Regression: fetch_all with a tag-pinned standalone NEVER touches HEAD or the lock.

    fetch_standalone only refreshes remote refs; it must NOT advance the working
    tree even if origin has moved. This test confirms the service calls
    fetch_standalone (network I/O) but does NOT call checkout or lock write.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref="v1.4.2")

    existing_entry = LockEntry(name="my-lib", ref="v1.4.2", kind=RefKind.tag, commit=SHA_OLD)
    lock_repo = FakeConfigLockRepository(entries={"my-lib": existing_entry})
    git_repo = FakeGitRepository()
    git_repo.head_commits[repo_path] = SHA_OLD

    repo_repo = _StandaloneSpyWriteRepoRepository()
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter_null: IFetchReporter = _NullFetchReporter()  # type: ignore[assignment]
    svc.fetch_all(
        scope=RepoScope.standalone,
        patterns=None,
        reporter=reporter_null,
    )

    # fetch_standalone was called (refreshes remote refs).
    assert repo_repo.fetched_standalones == ["my-lib"]
    # No checkouts.
    assert git_repo.detached_checkouts == []
    assert git_repo.branch_checkouts == []
    # Lock completely untouched.
    assert lock_repo.write_calls == []
    assert lock_repo.entries["my-lib"].commit == SHA_OLD


def test_branch_repin_preserves_other_repos_lock_entries(tmp_path: Path) -> None:
    """Other repos' lock entries are preserved across a branch re-pin rewrite.

    Read-modify-write: only this repo's entry changes; the other-repo entry
    must appear unchanged in the written snapshot.
    """
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()
    config = _make_standalone_workspace_config(tmp_path, "my-lib", ref="main")

    other_entry = LockEntry(name="other-repo", ref="v2.0", kind=RefKind.tag, commit="c" * 40)
    lock_repo = FakeConfigLockRepository(entries={"other-repo": other_entry})

    git_repo = FakeGitRepository()
    git_repo.resolved_refs[(repo_path, "main")] = (RefKind.branch, SHA_NEW)
    git_repo.clean_worktrees.add(repo_path)
    # HEAD is at SHA_NEW after the ff integration.
    git_repo.head_commits[repo_path] = SHA_NEW

    # Spy: integrate_standalone_to_ref returns fast_forwarded → lock will be rewritten.
    repo_repo = _StandaloneSpyWriteRepoRepository(integrate_to_ref_result=SyncResult.fast_forwarded)
    svc = _make_standalone_service(workspace, config, repo_repo, git_repo=git_repo, config_lock_repo=lock_repo)

    reporter = _RecordingPullReporter()
    svc.pull_all(
        scope=RepoScope.standalone,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    assert ("my-lib", "origin/main") in repo_repo.integrated_to_ref
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    # my-lib updated to new SHA.
    assert written["my-lib"].commit == SHA_NEW
    # other-repo entry preserved unchanged.
    assert written["other-repo"] == other_entry


@pytest.mark.parametrize("source_branch", ["master", None])
def test_pull_all_warns_and_still_integrates_when_source_checkout_is_off_main(
    workspace_config: WorkspaceConfig, tmp_path: Path, source_branch: str | None
) -> None:
    """An off-main source checkout emits one per-repo warning; worktree integration is unaffected."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    env_worktrees = _make_env_with_named_worktrees(workspace, tmp_path, ["repo-a"])
    repo_repo = _PullSpyWriteRepoRepository(upstreams={"repo-a": "origin/featbranch"}, source_branch=source_branch)
    svc = _make_pull_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter = _RecordingPullReporter()

    report = svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    assert len(reporter.source_warnings) == 1
    repo_name, warning = reporter.source_warnings[0]
    assert repo_name == "repo-a"
    assert "main_branch 'main'" in warning
    assert repo_repo.integrated == [("repo-a", "origin/featbranch")]
    assert report.success is True


def test_pull_all_on_main_source_checkout_emits_no_warning(workspace_config: WorkspaceConfig, tmp_path: Path) -> None:
    """A source checkout on its main branch produces no source-checkout warning."""
    workspace = Workspace(root_path=tmp_path, service_prefix="t", main_branch="main")
    env_worktrees = _make_env_with_named_worktrees(workspace, tmp_path, ["repo-a"])
    repo_repo = _PullSpyWriteRepoRepository(upstreams={"repo-a": "origin/featbranch"})
    svc = _make_pull_service(workspace, workspace_config, env_worktrees, repo_repo)
    reporter = _RecordingPullReporter()

    svc.pull_all(
        scope=RepoScope.project,
        patterns=None,
        mode=PullMode.ff_only,
        autostash=False,
        reporter=reporter,  # type: ignore[arg-type]
    )

    assert reporter.source_warnings == []
