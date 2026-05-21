from __future__ import annotations

from pathlib import Path

import git
import pytest

from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.write_repo_repository import WriteRepoRepository
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    RepoError,
    StandaloneRepository,
    Workspace,
)


@pytest.fixture
def error_factory() -> RepoErrorFactory:
    return RepoErrorFactory()


@pytest.fixture
def git_ops(error_factory: RepoErrorFactory) -> GitOpsService:
    return GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)


@pytest.fixture
def repo(error_factory: RepoErrorFactory, git_ops: GitOpsService) -> WriteRepoRepository:
    return WriteRepoRepository(error_factory=error_factory, git_ops=git_ops)


def _make_repo(path: Path) -> git.Repo:
    r = git.Repo.init(str(path), initial_branch="main")
    with r.config_writer(config_level="repository") as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")
    (path / "f.txt").write_text("hi")
    r.git.add("f.txt")
    r.git.commit("-m", "init")
    return r


def _wt(path: Path, name: str = "demo", main_branch: str = "main") -> FeatureWorktree:
    workspace = Workspace(root_path=path.parent, session_prefix="t", main_branch=main_branch)
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=path.parent)
    repo = ProjectRepository(name=name, main_path=path, main_branch=main_branch)
    return FeatureWorktree(workspace=workspace, environment=env, repository=repo)


def test_fetch_raises_structured_repo_error_on_missing_remote(tmp_path: Path, repo: WriteRepoRepository):
    repo_path = tmp_path / "demo"
    _make_repo(repo_path)
    # No 'origin' remote configured — fetch must fail with a structured RepoError.
    wt = _wt(repo_path)
    with pytest.raises(RepoError) as ei:
        repo.fetch(wt)
    err = ei.value
    assert err.subcommand == "fetch"
    assert "origin" in err.args
    assert err.cwd is not None and err.cwd.endswith("demo")
    assert err.exit_code is not None and err.exit_code != 0
    # stderr should carry git's diagnostic
    assert err.stderr  # non-empty


def test_count_commits_not_in_raises_for_bogus_ref(tmp_path: Path, repo: WriteRepoRepository):
    repo_path = tmp_path / "demo"
    _make_repo(repo_path)
    wt = _wt(repo_path)
    with pytest.raises(RepoError) as ei:
        repo.count_commits_not_in(wt, "refs/heads/does-not-exist")
    assert ei.value.subcommand == "rev-list"


def test_hard_reset_raises_for_bogus_ref(tmp_path: Path, repo: WriteRepoRepository):
    repo_path = tmp_path / "demo"
    _make_repo(repo_path)
    wt = _wt(repo_path)
    with pytest.raises(RepoError) as ei:
        repo.hard_reset(wt, "refs/heads/does-not-exist")
    assert ei.value.subcommand == "reset"


def test_push_standalone_raises_when_no_upstream(tmp_path: Path, repo: WriteRepoRepository):
    repo_path = tmp_path / "stand"
    _make_repo(repo_path)
    standalone = StandaloneRepository(name="stand", path=repo_path)
    with pytest.raises(RepoError) as ei:
        repo.push_standalone(standalone)
    # No GitPython call happened — message-only RepoError with cwd populated.
    assert "no upstream" in ei.value.message
    assert ei.value.cwd is not None


def test_sync_ff_only_raises_on_failure(tmp_path: Path, repo: WriteRepoRepository):
    repo_path = tmp_path / "demo"
    _make_repo(repo_path)
    project = ProjectRepository(name="demo", main_path=repo_path, main_branch="main")
    with pytest.raises(RepoError) as ei:
        repo.sync_ff_only(project)
    # Either fetch (no origin) or merge — both must surface a structured RepoError.
    assert ei.value.subcommand in {"fetch", "merge"}


def test_unset_upstream_is_idempotent_when_no_upstream(tmp_path: Path, repo: WriteRepoRepository):
    """`unset_upstream` on a branch with no upstream is a no-op, not an error.

    The repo has no upstream configured for `main`. The implementation must
    detect that via `git config --get` exit 1 and return without raising.
    """
    repo_path = tmp_path / "demo"
    _make_repo(repo_path)
    wt = _wt(repo_path)
    # Should not raise.
    repo.unset_upstream(wt)
