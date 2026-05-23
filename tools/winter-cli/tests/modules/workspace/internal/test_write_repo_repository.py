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


def test_count_commits_not_in_raises_for_bogus_ref(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_list.side_effect = git.GitCommandError(
        ("git", "rev-list", "--count"), 128, stderr=b"unknown revision"
    )
    wt = _wt(_REPO_PATH)

    with pytest.raises(RepoError) as ei:
        repo.count_commits_not_in(wt, "refs/heads/does-not-exist")

    assert ei.value.subcommand == "rev-list"


def test_hard_reset_raises_for_bogus_ref(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.reset.side_effect = git.GitCommandError(
        ("git", "reset", "--hard"), 128, stderr=b"ambiguous argument"
    )
    wt = _wt(_REPO_PATH)

    with pytest.raises(RepoError) as ei:
        repo.hard_reset(wt, "refs/heads/does-not-exist")

    assert ei.value.subcommand == "reset"


def test_push_standalone_raises_when_no_upstream(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.tracking_branch.return_value = None
    standalone = StandaloneRepository(name="stand", path=_STAND_PATH)

    with pytest.raises(RepoError) as ei:
        repo.push_standalone(standalone)

    assert "no upstream" in ei.value.message
    assert ei.value.cwd is not None


def test_sync_ff_only_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch, repo: WriteRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.fetch.side_effect = git.GitCommandError(
        ("git", "fetch", "origin"), 128, stderr=b"no such remote"
    )
    project = ProjectRepository(name="demo", main_path=_REPO_PATH, main_branch="main")

    with pytest.raises(RepoError) as ei:
        repo.sync_ff_only(project)

    assert ei.value.subcommand in {"fetch", "merge"}


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
