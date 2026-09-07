"""Real-git tests for the four read-only boundary probes: `get_ref_tip`,
`is_ancestor`, `fork_point`, and `is_rebase_in_progress`.

These exist to answer questions no fake can answer honestly: whether a rewrite
survives `git merge-base --fork-point`, whether git actually leaves a worktree
mid-rebase after a real conflicting replay, and whether `git.Repo(...)`'s
constructor-level failure for a missing worktree path is actually caught. Every
test here builds real commits with real subprocess git calls in `tmp_path`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from winter_cli.modules.workspace.internal.read_repo_repository import ReadRepoRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    Workspace,
)


@pytest.fixture
def repo() -> ReadRepoRepository:
    return ReadRepoRepository(RepoErrorFactory())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _init_repo(path: Path, default_branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", default_branch)
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "tester")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD").strip()


def _worktree(path: Path) -> FeatureWorktree:
    """Wires a `FeatureWorktree` back onto a real repo at `path`.

    `FeatureWorktree.path` is `environment.path / repository.name`, so
    `environment.path=path.parent` and `repository.name=path.name` makes the
    worktree resolve back to the real checkout, matching the trick
    `test_read_repo_repository.py`'s `_real_worktree` uses.
    """
    workspace = Workspace(root_path=path.parent, service_prefix="t", main_branch="main")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=path.parent)
    project = ProjectRepository(name=path.name, main_path=path, main_branch="main")
    return FeatureWorktree(workspace=workspace, environment=env, repository=project)


# --------------------------------------------------------------------------- #
# get_ref_tip
# --------------------------------------------------------------------------- #


def test_get_ref_tip_resolves_a_real_ref(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "f.txt", "a\n", "init")

    assert repo.get_ref_tip(_worktree(tmp_path), "HEAD") == sha
    assert repo.get_ref_tip(_worktree(tmp_path), "main") == sha


def test_get_ref_tip_returns_none_for_a_ref_absent_from_a_real_worktree(
    tmp_path: Path, repo: ReadRepoRepository
) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "a\n", "init")

    assert repo.get_ref_tip(_worktree(tmp_path), "refs/heads/does-not-exist") is None


def test_get_ref_tip_returns_none_for_a_worktree_path_absent_from_disk(
    tmp_path: Path, repo: ReadRepoRepository
) -> None:
    # `git.Repo(...)` raises `NoSuchPathError` from its constructor, outside
    # the inner `try` around `rev_parse` alone — this pins that the outer
    # `try` in `get_ref_tip` actually covers it, unlike `has_local_ref`
    # (`internal/write_repo_repository.py`), whose inner-only `try` would let
    # this escape as a raw exception.
    missing = tmp_path / "never-created"

    assert repo.get_ref_tip(_worktree(missing), "HEAD") is None


# --------------------------------------------------------------------------- #
# is_ancestor
# --------------------------------------------------------------------------- #


def test_is_ancestor_true_for_a_proper_ancestor(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "f.txt", "a\n", "base")
    _commit(tmp_path, "f.txt", "b\n", "second")

    assert repo.is_ancestor(_worktree(tmp_path), base, "HEAD") is True


def test_is_ancestor_true_for_two_identical_tips(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "f.txt", "a\n", "base")

    assert repo.is_ancestor(_worktree(tmp_path), sha, sha) is True
    assert repo.is_ancestor(_worktree(tmp_path), "HEAD", "main") is True


def test_is_ancestor_false_for_a_sibling_tip(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "a\n", "base")
    _git(tmp_path, "checkout", "-q", "-b", "sibling-one")
    _commit(tmp_path, "f.txt", "b\n", "on sibling-one")
    _git(tmp_path, "checkout", "-q", "-b", "sibling-two", "main")
    _commit(tmp_path, "f.txt", "c\n", "on sibling-two")

    assert repo.is_ancestor(_worktree(tmp_path), "sibling-one", "sibling-two") is False


# --------------------------------------------------------------------------- #
# fork_point — the three motivating rewrites, plus the no-fork-point case
# --------------------------------------------------------------------------- #


def _branch_stack(tmp_path: Path) -> str:
    """`env` forked from `pred` at `pred`'s tip; returns that tip's sha.

    The rewrites below move `pred` off this commit, so it survives only in
    `pred`'s reflog — which is the whole reason the boundary is derived with
    `--fork-point`. A plain `merge-base(pred, env)` answers `c0` once the
    commit leaves the graph: a real sha, and the wrong boundary, which would
    replay `pred`'s own commits into `env`.
    """
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "c0\n", "c0")
    _git(tmp_path, "checkout", "-q", "-b", "pred")
    forked_from = _commit(tmp_path, "f.txt", "c0\npred-1\n", "pred commit 1")
    _git(tmp_path, "checkout", "-q", "-b", "env")
    _commit(tmp_path, "g.txt", "env-1\n", "env commit 1")
    _commit(tmp_path, "g.txt", "env-1\nenv-2\n", "env commit 2")
    _git(tmp_path, "checkout", "-q", "pred")
    return forked_from


def test_fork_point_survives_commits_appended_to_the_predecessor(tmp_path: Path, repo: ReadRepoRepository) -> None:
    """Appending leaves the fork commit in the graph, so this case agrees with
    a plain merge-base. It pins the boundary against `pred` advancing, not
    against a rewrite."""
    forked_from = _branch_stack(tmp_path)
    _commit(tmp_path, "p.txt", "p\n", "pred appended")

    assert repo.fork_point(_worktree(tmp_path), "pred", "env") == forked_from


def test_fork_point_survives_an_amended_predecessor(tmp_path: Path, repo: ReadRepoRepository) -> None:
    forked_from = _branch_stack(tmp_path)
    _git(tmp_path, "commit", "-q", "--amend", "-m", "pred commit 1 (amended)")

    assert repo.fork_point(_worktree(tmp_path), "pred", "env") == forked_from
    # The commit env forked from is gone from pred's graph; only the reflog has it.
    assert repo.is_ancestor(_worktree(tmp_path), forked_from, "pred") is False


def test_fork_point_survives_a_locally_squashed_predecessor(tmp_path: Path, repo: ReadRepoRepository) -> None:
    forked_from = _branch_stack(tmp_path)
    _commit(tmp_path, "p.txt", "p\n", "pred commit 2")
    _git(tmp_path, "reset", "--soft", "main")
    _git(tmp_path, "commit", "-q", "-m", "pred squashed")

    assert repo.fork_point(_worktree(tmp_path), "pred", "env") == forked_from
    assert repo.is_ancestor(_worktree(tmp_path), forked_from, "pred") is False


def test_fork_point_returns_none_when_no_reflog_entry_qualifies(tmp_path: Path, repo: ReadRepoRepository) -> None:
    """Measured on git 2.43.0: once `pred`'s reflog is expired and it then
    moves again, `git merge-base --fork-point pred env` exits non-zero with
    empty output — no candidate in the reflog shares history with `env`
    anymore. `fork_point` must read that as `None` rather than falling back
    to a plain `merge-base(pred, env)`.

    Verified directly against this exact fixture: a plain `merge-base(pred,
    env)` actually still returns `forked_from` here — the *correct*
    boundary, not a wrong-but-plausible sha, since `forked_from` remains an
    ancestor of `pred`'s current tip even after the reflog that names it
    directly is gone. So this test does not demonstrate a degradation to a
    wrong answer; it pins a narrower, still-real contract: `fork_point`'s
    result is defined by what the reflog can show, not by whatever a
    fallback ancestry search would happen to find, and the two are allowed
    to disagree on which fixture would make them diverge — this fixture
    simply isn't one. `fork_point` must return `None` here regardless of
    what a fallback would compute.
    """
    _branch_stack(tmp_path)
    _commit(tmp_path, "p.txt", "p\n", "pred original")
    _git(tmp_path, "reflog", "expire", "--expire=now", "--all")
    _commit(tmp_path, "p.txt", "p\np2\n", "pred moved again")

    assert repo.fork_point(_worktree(tmp_path), "pred", "env") is None


# --------------------------------------------------------------------------- #
# is_rebase_in_progress — driven by a real conflicting replay, not a fake
# --------------------------------------------------------------------------- #


def test_is_rebase_in_progress_true_when_a_conflicting_replay_stopped_it(
    tmp_path: Path, repo: ReadRepoRepository
) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "f.txt", "base\n", "base")
    _git(tmp_path, "branch", "pred")
    _git(tmp_path, "checkout", "-q", "-b", "env")
    _commit(tmp_path, "f.txt", "base\nenv\n", "env commit")
    _git(tmp_path, "checkout", "-q", "pred")
    _commit(tmp_path, "f.txt", "base\npred\n", "pred commit")

    result = subprocess.run(
        ["git", "rebase", "--onto", "pred", base, "env"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "fixture setup must actually conflict"

    assert repo.is_rebase_in_progress(_worktree(tmp_path)) is True


def test_is_rebase_in_progress_false_once_continued(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "f.txt", "base\n", "base")
    _git(tmp_path, "branch", "pred")
    _git(tmp_path, "checkout", "-q", "-b", "env")
    _commit(tmp_path, "f.txt", "base\nenv\n", "env commit")
    _git(tmp_path, "checkout", "-q", "pred")
    _commit(tmp_path, "f.txt", "base\npred\n", "pred commit")

    subprocess.run(
        ["git", "rebase", "--onto", "pred", base, "env"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert repo.is_rebase_in_progress(_worktree(tmp_path)) is True

    (tmp_path / "f.txt").write_text("base\npred\nenv\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "-c", "core.editor=true", "rebase", "--continue")

    assert repo.is_rebase_in_progress(_worktree(tmp_path)) is False


def test_is_rebase_in_progress_false_for_a_clean_worktree(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "a\n", "init")

    assert repo.is_rebase_in_progress(_worktree(tmp_path)) is False
