"""Real-git tests for `rebase_onto` — the `git rebase --onto` adapter that,
unlike every other rebase/merge path in `WriteRepoRepository`, never aborts on
conflict.

Mocking `git.Repo` would only assert that `r.git.rebase(...)` was called with
the right arguments; the contract that matters here — a conflicting replay
really does leave the worktree mid-rebase, with the replayed commit and
conflicted paths readable off disk — can only be pinned against a real git
process. Every test builds actual commits with GitPython's index API in
`tmp_path`, matching `test_write_repo_repository_local_ff.py`'s convention.
"""

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
    Workspace,
)


@pytest.fixture
def repo_svc() -> WriteRepoRepository:
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    return WriteRepoRepository(error_factory=error_factory, git_ops=git_ops)


def _configure(r: git.Repo) -> git.Repo:
    with r.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")
        cw.set_value("commit", "gpgsign", "false")
    return r


def _working_dir(r: git.Repo) -> Path:
    wtd = r.working_tree_dir
    assert wtd is not None, "test fixture initialized repo without a working tree"
    return Path(str(wtd))


def _commit(r: git.Repo, file_name: str, content: str, message: str) -> str:
    path = _working_dir(r) / file_name
    path.write_text(content)
    r.index.add([file_name])
    return r.index.commit(message).hexsha


def _worktree(path: Path) -> FeatureWorktree:
    workspace = Workspace(root_path=path.parent, service_prefix="t", main_branch="main")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=path.parent)
    project = ProjectRepository(name=path.name, main_path=path, main_branch="main")
    return FeatureWorktree(workspace=workspace, environment=env, repository=project)


def test_rebase_onto_clean_replay(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    r = _configure(git.Repo.init(str(tmp_path / "demo"), initial_branch="main"))
    _commit(r, "f.txt", "base\n", "base")
    r.git.branch("pred")
    r.git.checkout("-b", "env")
    _commit(r, "env-only.txt", "env\n", "env commit")
    r.git.checkout("pred")
    _commit(r, "pred-only.txt", "pred\n", "pred commit")
    pred_tip = r.git.rev_parse("pred")

    result = repo_svc.rebase_onto(_worktree(tmp_path / "demo"), newbase="pred", oldbase="main", branch="env")

    assert result.success is True
    assert result.conflict is None
    # env's own commit replayed on top of pred's new tip; pred-only.txt (from
    # pred) and env-only.txt (env's own commit) both present, no conflict markers.
    assert r.git.rev_parse("env^") == pred_tip
    assert (tmp_path / "demo" / "pred-only.txt").exists()
    assert (tmp_path / "demo" / "env-only.txt").exists()
    assert r.active_branch.name == "env"


def test_rebase_onto_zero_commit_fast_forward(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """An env carrying nothing past its boundary fast-forwards cleanly —
    the zero-commit case `rebase_onto`'s clean path must also cover."""
    r = _configure(git.Repo.init(str(tmp_path / "demo"), initial_branch="main"))
    _commit(r, "f.txt", "base\n", "base")
    r.git.branch("pred")
    r.git.branch("env")  # env carries nothing past main
    r.git.checkout("pred")
    _commit(r, "pred-only.txt", "pred\n", "pred commit")
    pred_tip = r.git.rev_parse("pred")

    result = repo_svc.rebase_onto(_worktree(tmp_path / "demo"), newbase="pred", oldbase="main", branch="env")

    assert result.success is True
    assert r.git.rev_parse("env") == pred_tip


def test_rebase_onto_conflict_leaves_repo_mid_rebase_with_detail(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    r = _configure(git.Repo.init(str(tmp_path / "demo"), initial_branch="main"))
    _commit(r, "f.txt", "base\n", "base")
    r.git.branch("pred")
    r.git.checkout("-b", "env")
    env_commit = _commit(r, "f.txt", "base\nenv\n", "env commit")
    r.git.checkout("pred")
    _commit(r, "f.txt", "base\npred\n", "pred commit")

    result = repo_svc.rebase_onto(_worktree(tmp_path / "demo"), newbase="pred", oldbase="main", branch="env")

    assert result.success is False
    assert result.conflict is not None
    assert result.conflict.replayed_commit == env_commit
    assert result.conflict.conflicted_paths == ["f.txt"]
    # Left mid-rebase, not aborted — the opposite postcondition from
    # `_ff_or_rebase`, which would have run `git rebase --abort` here.
    assert (_working_dir(r) / r.git.rev_parse("--git-path", "rebase-merge")).exists()


def test_rebase_onto_conflict_under_apply_backend_leaves_repo_mid_rebase_with_detail(
    tmp_path: Path, repo_svc: WriteRepoRepository
) -> None:
    """The same conflicting replay as
    `test_rebase_onto_conflict_leaves_repo_mid_rebase_with_detail`, forced
    onto the older apply backend (`rebase.backend=apply`) rather than git's
    merge-backend default. A conflicting `git rebase --onto` under this
    backend exits 1 and leaves `.git/rebase-apply/` — never
    `rebase-merge/`, which the merge-backend test above asserts — so a
    `_read_rebase_conflict` that only ever reads `rebase-merge/stopped-sha`
    finds nothing and would make `rebase_onto` raise here instead of
    returning a `RebaseOntoResult` with the conflict detail. Both backend
    tests must pass; neither backend is dropped in favor of the other.
    """
    r = _configure(git.Repo.init(str(tmp_path / "demo"), initial_branch="main"))
    r.git.config("rebase.backend", "apply")
    _commit(r, "f.txt", "base\n", "base")
    r.git.branch("pred")
    r.git.checkout("-b", "env")
    env_commit = _commit(r, "f.txt", "base\nenv\n", "env commit")
    r.git.checkout("pred")
    _commit(r, "f.txt", "base\npred\n", "pred commit")

    result = repo_svc.rebase_onto(_worktree(tmp_path / "demo"), newbase="pred", oldbase="main", branch="env")

    assert result.success is False
    assert result.conflict is not None
    assert result.conflict.replayed_commit == env_commit
    assert result.conflict.conflicted_paths == ["f.txt"]
    # The apply backend's own state directory — never rebase-merge/, which
    # this backend never creates.
    assert (_working_dir(r) / r.git.rev_parse("--git-path", "rebase-apply")).exists()
    assert not (_working_dir(r) / r.git.rev_parse("--git-path", "rebase-merge")).exists()


def test_rebase_onto_conflict_then_continue_completes_the_rebase(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    r = _configure(git.Repo.init(str(tmp_path / "demo"), initial_branch="main"))
    _commit(r, "f.txt", "base\n", "base")
    r.git.branch("pred")
    r.git.checkout("-b", "env")
    _commit(r, "f.txt", "base\nenv\n", "env commit")
    r.git.checkout("pred")
    _commit(r, "f.txt", "base\npred\n", "pred commit")

    result = repo_svc.rebase_onto(_worktree(tmp_path / "demo"), newbase="pred", oldbase="main", branch="env")
    assert result.success is False

    (tmp_path / "demo" / "f.txt").write_text("base\npred\nenv\n")
    r.git.add("f.txt")
    with r.git.custom_environment(GIT_EDITOR="true"):
        r.git.rebase("--continue")

    assert not (_working_dir(r) / r.git.rev_parse("--git-path", "rebase-merge")).exists()
    assert (tmp_path / "demo" / "f.txt").read_text() == "base\npred\nenv\n"
