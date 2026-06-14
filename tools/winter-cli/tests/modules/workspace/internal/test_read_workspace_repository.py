from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest

from winter_cli.modules.workspace.env_index import GREEK_LETTERS, resolve_env_index
from winter_cli.modules.workspace.internal import read_workspace_repository
from winter_cli.modules.workspace.internal.read_workspace_repository import (
    ReadWorkspaceRepository,
)
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import ProjectRepository, Workspace

_ROOT = Path("/fake/workspace")


def _fake_git_repo(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    git_mock = MagicMock()
    git_mock.GitCommandError = git.GitCommandError
    git_mock.InvalidGitRepositoryError = git.InvalidGitRepositoryError
    git_mock.NoSuchPathError = git.NoSuchPathError
    # The implementation uses `with git.Repo(...) as r:`, so __enter__ must return
    # the same mock that tests assert against.
    git_mock.Repo.return_value.__enter__.return_value = git_mock.Repo.return_value
    monkeypatch.setattr(read_workspace_repository, "git", git_mock)
    return git_mock


@pytest.fixture
def repo() -> ReadWorkspaceRepository:
    return ReadWorkspaceRepository(RepoErrorFactory())


def _project(name: str) -> ProjectRepository:
    return ProjectRepository(
        name=name,
        main_path=_ROOT / "projects" / name,
        main_branch="main",
    )


def test_resolve_env_index_for_greek_letter_is_fixed() -> None:
    assert resolve_env_index("alpha") == 1
    assert resolve_env_index("beta") == 2
    assert resolve_env_index("omega") == len(GREEK_LETTERS)


def test_resolve_env_index_for_non_greek_is_deterministic() -> None:
    first = resolve_env_index("feature-x")
    second = resolve_env_index("feature-x")
    assert first == second
    assert 26 <= first < 26 + 256


def test_get_environments_discovers_greek_dirs_with_known_repos(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")

    # alpha/ and gamma/ contain "demo"; delta/ contains only "other".
    def _is_dir(self: Path) -> bool:
        existing = {
            _ROOT / "alpha",
            _ROOT / "alpha" / "demo",
            _ROOT / "gamma",
            _ROOT / "gamma" / "demo",
            _ROOT / "delta",
            _ROOT / "delta" / "other",
        }
        return self in existing

    def _iterdir(self: Path) -> list[Path]:
        contents: dict[Path, list[Path]] = {
            _ROOT / "alpha": [_ROOT / "alpha" / "demo"],
            _ROOT / "gamma": [_ROOT / "gamma" / "demo"],
            _ROOT / "delta": [_ROOT / "delta" / "other"],
        }
        return contents.get(self, [])

    monkeypatch.setattr(Path, "is_dir", _is_dir)
    monkeypatch.setattr(Path, "iterdir", _iterdir)

    envs = repo.get_environments(workspace, [_project("demo")])

    names = [e.name for e in envs]
    assert names == ["alpha", "gamma"]
    assert envs[0].index == 1
    assert envs[1].index == 3


def test_get_environment_status_reads_feature_branch_from_tracking(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")
    worktree_path = _ROOT / "alpha" / "demo"

    # The adapter checks (worktree_path / ".git").exists()
    monkeypatch.setattr(Path, "exists", lambda self: self == worktree_path / ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    r.git.config.side_effect = ["origin", "refs/heads/feature/widget"]

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("demo")])

    assert status.feature_branch == "feature/widget"


def test_get_environment_status_returns_none_when_no_tracking(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")
    worktree_path = _ROOT / "alpha" / "demo"

    monkeypatch.setattr(Path, "exists", lambda self: self == worktree_path / ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    config_not_found = git.GitCommandError(("git", "config", "--get"), 1, stderr=b"")
    config_not_found.status = 1
    r.git.config.side_effect = config_not_found

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("demo")])

    assert status.feature_branch is None
    assert status.distinct_remote_count == 0


def test_get_environment_status_counts_distinct_remotes_across_worktrees(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    """Two non-pinned worktrees on different branches → primary is the first, distinct count is 2."""
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")

    monkeypatch.setattr(Path, "exists", lambda self: self.name == ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    # Two config reads (remote, merge) per worktree, in repo order: api, web.
    r.git.config.side_effect = [
        "origin",
        "refs/heads/feature/auth",
        "origin",
        "refs/heads/feature/billing",
    ]

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("api"), _project("web")])

    assert status.feature_branch == "feature/auth"
    assert status.distinct_remote_count == 2


def test_get_environment_status_shared_branch_counts_one_distinct_remote(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    """Two non-pinned worktrees on the *same* branch → a single distinct remote (no `+N`)."""
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")

    monkeypatch.setattr(Path, "exists", lambda self: self.name == ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    r.git.config.side_effect = [
        "origin",
        "refs/heads/feature/widget",
        "origin",
        "refs/heads/feature/widget",
    ]

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("api"), _project("web")])

    assert status.feature_branch == "feature/widget"
    assert status.distinct_remote_count == 1


def test_get_environment_status_primary_is_first_connected_repo(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    """When the leading non-pinned repo is disconnected, the primary is the next connected one."""
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")

    monkeypatch.setattr(Path, "exists", lambda self: self.name == ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    # First repo (api) has no upstream config (exit 1); second repo (web) tracks a branch.
    config_not_found = git.GitCommandError(("git", "config", "--get"), 1, stderr=b"")
    config_not_found.status = 1
    r.git.config.side_effect = [
        config_not_found,  # api: branch.alpha.remote not set
        "origin",  # web: remote
        "refs/heads/feature/web",  # web: merge
    ]

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("api"), _project("web")])

    assert status.feature_branch == "feature/web"
    assert status.distinct_remote_count == 1


def test_get_environment_status_excludes_pinned_from_distinct_count(
    monkeypatch: pytest.MonkeyPatch, repo: ReadWorkspaceRepository
) -> None:
    """A pinned repo tracks main and is never read — it doesn't inflate the distinct count."""
    git_mock = _fake_git_repo(monkeypatch)
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")

    monkeypatch.setattr(Path, "exists", lambda self: self.name == ".git")

    r = git_mock.Repo.return_value
    r.active_branch.name = "alpha"
    # Only the single non-pinned repo is read (two config calls).
    r.git.config.side_effect = ["origin", "refs/heads/feature/auth"]

    pinned = ProjectRepository(
        name="pinned", main_path=_ROOT / "projects" / "pinned", main_branch="main", pinned=True
    )

    env = repo.get_environment(workspace, "alpha")
    status = repo.get_environment_status(env, [_project("api"), pinned])

    assert status.feature_branch == "feature/auth"
    assert status.distinct_remote_count == 1
