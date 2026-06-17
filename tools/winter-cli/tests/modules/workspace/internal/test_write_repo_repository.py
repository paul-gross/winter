from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest

from winter_cli.modules.workspace.internal import read_repo_repository, write_repo_repository
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

_ROOT = Path("/fake/workspace")
_REPO_PATH = _ROOT / "demo"
_STAND_PATH = _ROOT / "stand"


def _fake_git_repo(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    git_mock = MagicMock()
    git_mock.GitCommandError = git.GitCommandError
    git_mock.InvalidGitRepositoryError = git.InvalidGitRepositoryError
    git_mock.NoSuchPathError = git.NoSuchPathError
    # The implementation uses `with git.Repo(...) as r:`, so __enter__ must return
    # the same mock that tests assert against.
    git_mock.Repo.return_value.__enter__.return_value = git_mock.Repo.return_value
    monkeypatch.setattr(write_repo_repository, "git", git_mock)
    monkeypatch.setattr(read_repo_repository, "git", git_mock)
    return git_mock


@pytest.fixture
def error_factory() -> RepoErrorFactory:
    return RepoErrorFactory()


@pytest.fixture
def git_ops(error_factory: RepoErrorFactory) -> GitOpsService:
    return GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)


@pytest.fixture
def repo(error_factory: RepoErrorFactory, git_ops: GitOpsService) -> WriteRepoRepository:
    return WriteRepoRepository(error_factory=error_factory, git_ops=git_ops)


def _wt(path: Path, name: str = "demo", main_branch: str = "main") -> FeatureWorktree:
    workspace = Workspace(root_path=path.parent, session_prefix="t", main_branch=main_branch)
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=path.parent)
    project_repo = ProjectRepository(name=name, main_path=path, main_branch=main_branch)
    return FeatureWorktree(workspace=workspace, environment=env, repository=project_repo)


def test_fetch_raises_structured_repo_error_on_missing_remote(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.fetch.side_effect = git.GitCommandError(
        ("git", "fetch", "origin"), 128, stderr=b"no such remote 'origin'"
    )
    wt = _wt(_REPO_PATH)

    with pytest.raises(RepoError) as ei:
        repo.fetch(wt)

    err = ei.value
    assert err.subcommand == "fetch"
    assert "origin" in err.cmd_args
    assert err.cwd is not None and "demo" in err.cwd
    assert err.exit_code is not None and err.exit_code != 0
    assert err.stderr


def test_count_commits_not_in_raises_for_bogus_ref(monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_list.side_effect = git.GitCommandError(
        ("git", "rev-list", "--count"), 128, stderr=b"unknown revision"
    )
    wt = _wt(_REPO_PATH)

    with pytest.raises(RepoError) as ei:
        repo.count_commits_not_in(wt, "refs/heads/does-not-exist")

    assert ei.value.subcommand == "rev-list"


def test_hard_reset_raises_for_bogus_ref(monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.reset.side_effect = git.GitCommandError(
        ("git", "reset", "--hard"), 128, stderr=b"ambiguous argument"
    )
    wt = _wt(_REPO_PATH)

    with pytest.raises(RepoError) as ei:
        repo.hard_reset(wt, "refs/heads/does-not-exist")

    assert ei.value.subcommand == "reset"


def test_push_standalone_raises_when_no_upstream(monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.tracking_branch.return_value = None
    standalone = StandaloneRepository(name="stand", path=_STAND_PATH)

    with pytest.raises(RepoError) as ei:
        repo.push_standalone(standalone)

    assert "no upstream" in ei.value.message
    assert ei.value.cwd is not None


def test_sync_ff_only_raises_on_failure(monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.fetch.side_effect = git.GitCommandError(
        ("git", "fetch", "origin"), 128, stderr=b"no such remote"
    )
    project = ProjectRepository(name="demo", main_path=_REPO_PATH, main_branch="main")

    with pytest.raises(RepoError) as ei:
        repo.sync_ff_only(project)

    assert ei.value.subcommand in {"fetch", "merge"}


def test_push_returns_commit_count_against_remote_ref_when_present(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    """Push count is computed from origin/<feature_branch>..HEAD when the remote ref exists.

    Regression: a feature with 14 commits already on `origin/feature/foo` plus
    1 fresh commit must report `1` pushed, not `15`.
    """
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    # rev_parse succeeds → remote ref exists
    r.git.rev_parse.return_value = "abc123"
    # rev_list returns the count of commits in the range
    r.git.rev_list.return_value = "1"
    wt = _wt(_REPO_PATH)

    result = repo.push(wt, feature_branch="feature/foo")

    assert result == 1
    # Must have checked for the remote ref...
    r.git.rev_parse.assert_called_with("--verify", "--quiet", "origin/feature/foo")
    # ...and counted HEAD..origin/feature/foo
    r.git.rev_list.assert_called_with("--count", "origin/feature/foo..HEAD")


def test_push_falls_back_to_main_branch_count_when_no_remote_ref(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    """First push to a new remote branch: count commits past origin/<main_branch>."""
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    # rev_parse raises → remote ref does not exist yet
    r.git.rev_parse.side_effect = git.GitCommandError(("git", "rev-parse"), 128, stderr=b"unknown")
    r.git.rev_list.return_value = "3"
    wt = _wt(_REPO_PATH)

    result = repo.push(wt, feature_branch="feature/foo")

    assert result == 3
    r.git.rev_list.assert_called_with("--count", "origin/main..HEAD")


def test_unset_upstream_is_idempotent_when_no_upstream(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    r.active_branch.name = "main"
    config_not_found = git.GitCommandError(("git", "config", "--get"), 1, stderr=b"")
    config_not_found.status = 1
    r.git.config.side_effect = config_not_found
    wt = _wt(_REPO_PATH)

    repo.unset_upstream(wt)

    r.git.branch.assert_not_called()


def test_get_worktree_upstream_returns_tracking_branch_name(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    tb = MagicMock()
    tb.name = "origin/feature-123"
    r.active_branch.tracking_branch.return_value = tb
    wt = _wt(_REPO_PATH)

    assert repo.get_worktree_upstream(wt) == "origin/feature-123"


def test_get_worktree_upstream_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    r.active_branch.tracking_branch.return_value = None
    wt = _wt(_REPO_PATH)

    assert repo.get_worktree_upstream(wt) is None


def test_get_worktree_push_branch_reads_bare_branch_from_config(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    """Resolves the bare push branch from config — works even with no remote-tracking ref.

    Reads `branch.<head>.{remote,merge}` directly rather than via
    `tracking_branch()`, so a freshly connected, never-fetched feature
    branch (the first-push case) still resolves a target.
    """
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    r.active_branch.tracking_branch.return_value = None  # never fetched: no remote-tracking ref
    r.git.config.side_effect = ["origin", "refs/heads/feature/never-fetched"]
    wt = _wt(_REPO_PATH)

    assert repo.get_worktree_push_branch(wt) == "feature/never-fetched"


def test_get_worktree_push_branch_returns_none_when_no_upstream(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    config_not_found = git.GitCommandError(("git", "config", "--get"), 1, stderr=b"")
    config_not_found.status = 1
    r.git.config.side_effect = config_not_found
    wt = _wt(_REPO_PATH)

    assert repo.get_worktree_push_branch(wt) is None


def test_get_worktree_push_branch_returns_none_for_non_origin_remote(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    r.git.config.side_effect = ["upstream", "refs/heads/feature/x"]
    wt = _wt(_REPO_PATH)

    assert repo.get_worktree_push_branch(wt) is None
