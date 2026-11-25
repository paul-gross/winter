from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, call

import git
import pytest

from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
from winter_cli.modules.workspace.internal import gitpython_repository
from winter_cli.modules.workspace.internal.gitpython_repository import GitPythonRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.models.domain_model import RefKind

_REPO_PATH = Path("/fake/repo")
_SOURCE_PATH = Path("/fake/source")
_WT_PATH = Path("/fake/alpha/repo")
_DEST_PATH = Path("/fake/dest")


def _fake_git_repo(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    git_mock = MagicMock()
    # Keep real exception classes so the adapter's except clauses resolve correctly.
    git_mock.GitCommandError = git.GitCommandError
    git_mock.InvalidGitRepositoryError = git.InvalidGitRepositoryError
    git_mock.NoSuchPathError = git.NoSuchPathError
    # The implementation uses `with git.Repo(...) as r:`, so __enter__ must return
    # the same mock that tests assert against.
    git_mock.Repo.return_value.__enter__.return_value = git_mock.Repo.return_value
    monkeypatch.setattr(gitpython_repository, "git", git_mock)
    return git_mock


@pytest.fixture
def adapter() -> GitPythonRepository:
    return GitPythonRepository(RepoErrorFactory(), NoopCommandTracer())


# ── clone ──────────────────────────────────────────────────────────────────


def test_clone_calls_clone_from_with_url_and_dest(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.clone("git@example.com:org/repo.git", _DEST_PATH, repo_name="repo")

    git_mock.Repo.clone_from.assert_called_once_with("git@example.com:org/repo.git", str(_DEST_PATH), env=None)


def test_clone_passes_the_clone_env(monkeypatch: pytest.MonkeyPatch) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    adapter = GitPythonRepository(RepoErrorFactory(), NoopCommandTracer(), clone_env={"GIT_TERMINAL_PROMPT": "0"})

    adapter.clone("git@example.com:org/repo.git", _DEST_PATH, repo_name="repo")

    git_mock.Repo.clone_from.assert_called_once_with(
        "git@example.com:org/repo.git", str(_DEST_PATH), env={"GIT_TERMINAL_PROMPT": "0"}
    )


def test_clone_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.clone_from.side_effect = git.GitCommandError(("git", "clone", "origin"), 128, stderr=b"not found")

    with pytest.raises(RepoError) as ei:
        adapter.clone("git@example.com:org/repo.git", _DEST_PATH, repo_name="repo")

    assert "clone failed" in ei.value.message
    assert ei.value.subcommand == "clone"


def test_clone_with_branch_keeps_the_plain_message_for_an_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.clone_from.side_effect = git.GitCommandError(
        ("git", "clone", "origin"), 128, stderr=b"fatal: Could not read from remote repository."
    )

    with pytest.raises(RepoError) as ei:
        adapter.clone("git@example.com:org/repo.git", _DEST_PATH, repo_name="repo", branch="rust-cli")

    assert ei.value.message == "clone failed for git@example.com:org/repo.git"


# ── clone against real git ─────────────────────────────────────────────────
#
# `--branch` semantics (which branch HEAD lands on, its upstream, and git's
# cleanup of `dest` on a missing branch) are git's behavior, so these build a
# real bare `origin` whose default HEAD is `master` with a second `rust-cli`
# branch ahead of it.


def _origin_with_two_branches(tmp_path: Path) -> Path:
    seed = git.Repo.init(tmp_path / "seed", initial_branch="master")
    with seed.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")
        cw.set_value("commit", "gpgsign", "false")
    (tmp_path / "seed" / "a.txt").write_text("a")
    seed.index.add(["a.txt"])
    seed.index.commit("master commit")
    seed.git.checkout("-b", "rust-cli")
    (tmp_path / "seed" / "b.txt").write_text("b")
    seed.index.add(["b.txt"])
    seed.index.commit("rust-cli commit")
    seed.git.checkout("master")
    origin = tmp_path / "origin.git"
    git.Repo.clone_from(str(tmp_path / "seed"), str(origin), bare=True)
    seed.close()
    return origin


def _head_and_upstream(path: Path) -> tuple[str, str]:
    with git.Repo(str(path)) as r:
        return r.active_branch.name, r.git.rev_parse("--abbrev-ref", "@{upstream}")


def test_clone_checks_out_a_non_default_branch_tracking_its_origin(
    tmp_path: Path, adapter: GitPythonRepository
) -> None:
    origin = _origin_with_two_branches(tmp_path)
    dest = tmp_path / "projects" / "repo"

    adapter.clone(str(origin), dest, repo_name="repo", branch="rust-cli")

    assert _head_and_upstream(dest) == ("rust-cli", "origin/rust-cli")


def test_clone_of_the_default_branch_lands_on_it_as_before(tmp_path: Path, adapter: GitPythonRepository) -> None:
    origin = _origin_with_two_branches(tmp_path)
    with_branch = tmp_path / "with-branch"
    without_branch = tmp_path / "without-branch"

    adapter.clone(str(origin), with_branch, repo_name="repo", branch="master")
    adapter.clone(str(origin), without_branch, repo_name="repo")

    assert _head_and_upstream(with_branch) == ("master", "origin/master")
    assert _head_and_upstream(with_branch) == _head_and_upstream(without_branch)


def test_clone_of_a_branch_missing_on_the_remote_raises_and_leaves_no_checkout(
    tmp_path: Path, adapter: GitPythonRepository
) -> None:
    origin = _origin_with_two_branches(tmp_path)
    dest = tmp_path / "projects" / "repo"
    dest.parent.mkdir()

    with pytest.raises(RepoError) as ei:
        adapter.clone(str(origin), dest, repo_name="repo", branch="no-such-branch")

    assert "branch 'no-such-branch' not found on the remote" in ei.value.message
    assert ei.value.subcommand == "clone"
    assert not dest.exists()


# ── add_worktree ───────────────────────────────────────────────────────────


def test_add_worktree_without_base_branch_calls_worktree_add(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.add_worktree(_SOURCE_PATH, _WT_PATH, branch="alpha", repo_name="repo", env=None)

    git_mock.Repo.assert_called_once_with(str(_SOURCE_PATH))
    git_mock.Repo.return_value.git.worktree.assert_called_once_with("add", str(_WT_PATH), "alpha")


def test_add_worktree_with_base_branch_passes_b_flag(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.add_worktree(_SOURCE_PATH, _WT_PATH, branch="alpha", base_branch="main", repo_name="repo", env=None)

    git_mock.Repo.return_value.git.worktree.assert_called_once_with(
        "add", str(_WT_PATH), "-b", "alpha", "--no-track", "main"
    )


def test_add_worktree_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.worktree.side_effect = git.GitCommandError(
        ("git", "worktree", "add"), 128, stderr=b"already exists"
    )

    with pytest.raises(RepoError) as ei:
        adapter.add_worktree(_SOURCE_PATH, _WT_PATH, branch="alpha", repo_name="repo", env=None)

    assert "worktree add failed" in ei.value.message
    assert ei.value.subcommand == "worktree"


# ── remove_worktree ────────────────────────────────────────────────────────


def test_remove_worktree_without_force_calls_remove(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.remove_worktree(_SOURCE_PATH, _WT_PATH, force=False, repo_name="repo", env=None)

    git_mock.Repo.return_value.git.worktree.assert_called_once_with("remove", str(_WT_PATH))


def test_remove_worktree_with_force_passes_force_flag(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.remove_worktree(_SOURCE_PATH, _WT_PATH, force=True, repo_name="repo", env=None)

    git_mock.Repo.return_value.git.worktree.assert_called_once_with("remove", "--force", str(_WT_PATH))


def test_remove_worktree_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.worktree.side_effect = git.GitCommandError(
        ("git", "worktree", "remove"), 128, stderr=b"dirty"
    )

    with pytest.raises(RepoError) as ei:
        adapter.remove_worktree(_SOURCE_PATH, _WT_PATH, force=False, repo_name="repo", env=None)

    assert "worktree remove failed" in ei.value.message


# ── list_worktrees ─────────────────────────────────────────────────────────


def test_list_worktrees_parses_porcelain_output(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.worktree.return_value = (
        "worktree /repo/main\nHEAD abc123\nbranch refs/heads/main\n\n"
        "worktree /repo/alpha\nHEAD def456\nbranch refs/heads/alpha\n"
    )

    result = adapter.list_worktrees(_SOURCE_PATH)

    assert result == [Path("/repo/main"), Path("/repo/alpha")]
    git_mock.Repo.return_value.git.worktree.assert_called_once_with("list", "--porcelain")


def test_list_worktrees_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.worktree.side_effect = git.GitCommandError(
        ("git", "worktree", "list"), 128, stderr=b"not a git repo"
    )

    with pytest.raises(RepoError) as ei:
        adapter.list_worktrees(_SOURCE_PATH)

    assert "worktree list failed" in ei.value.message


# ── get_local_branches ─────────────────────────────────────────────────────


def test_get_local_branches_returns_head_names(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    head_a = MagicMock()
    head_a.name = "main"
    head_b = MagicMock()
    head_b.name = "alpha"
    git_mock.Repo.return_value.heads = [head_a, head_b]

    result = adapter.get_local_branches(_REPO_PATH, repo_name="repo", env=None)

    assert result == ["main", "alpha"]


def test_get_local_branches_returns_empty_for_no_heads(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.heads = []

    result = adapter.get_local_branches(_REPO_PATH, repo_name="repo", env=None)

    assert result == []


# ── get_tracking_branch ────────────────────────────────────────────────────


def test_get_tracking_branch_returns_name_when_set(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    tb = MagicMock()
    tb.name = "origin/main"
    git_mock.Repo.return_value.active_branch.tracking_branch.return_value = tb

    result = adapter.get_tracking_branch(_REPO_PATH, repo_name="repo", env=None)

    assert result == "origin/main"


def test_get_tracking_branch_returns_none_when_not_set(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.tracking_branch.return_value = None

    result = adapter.get_tracking_branch(_REPO_PATH, repo_name="repo", env=None)

    assert result is None


def test_get_tracking_branch_returns_none_on_type_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.tracking_branch.side_effect = TypeError("detached HEAD")

    result = adapter.get_tracking_branch(_REPO_PATH, repo_name="repo", env=None)

    assert result is None


# ── set_upstream_to ────────────────────────────────────────────────────────


def test_set_upstream_to_writes_tracking_config_directly(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.name = "alpha"

    adapter.set_upstream_to(_REPO_PATH, "origin/main", repo_name="repo", env=None)

    # Written directly (not via `git branch --set-upstream-to`) so tracking can
    # be set to a remote branch git cannot yet resolve locally (unpushed feature).
    git_mock.Repo.return_value.git.branch.assert_not_called()
    git_mock.Repo.return_value.git.config.assert_any_call("branch.alpha.remote", "origin")
    git_mock.Repo.return_value.git.config.assert_any_call("branch.alpha.merge", "refs/heads/main")


def test_set_upstream_to_tolerates_unpushed_remote_branch(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.name = "alpha"

    # A nested, never-pushed feature branch: `--set-upstream-to` would exit 128,
    # but writing config directly must succeed and split remote/branch correctly.
    adapter.set_upstream_to(_REPO_PATH, "origin/feature/foo", repo_name="repo", env=None)

    git_mock.Repo.return_value.git.config.assert_any_call("branch.alpha.remote", "origin")
    git_mock.Repo.return_value.git.config.assert_any_call("branch.alpha.merge", "refs/heads/feature/foo")


def test_set_upstream_to_rejects_ref_without_remote_prefix(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    _fake_git_repo(monkeypatch)

    with pytest.raises(RepoError):
        adapter.set_upstream_to(_REPO_PATH, "bare-ref", repo_name="repo", env=None)


def test_set_upstream_to_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.active_branch.name = "alpha"
    git_mock.Repo.return_value.git.config.side_effect = git.GitCommandError(("git", "config"), 128, stderr=b"boom")

    with pytest.raises(RepoError) as ei:
        adapter.set_upstream_to(_REPO_PATH, "origin/main", repo_name="repo", env=None)

    assert "set-upstream-to" in ei.value.message


def test_set_upstream_to_raises_repo_error_on_detached_head(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    """A detached HEAD makes GitPython's `active_branch` raise `TypeError`.

    `set_upstream_to` must convert that into a `RepoError` naming the worktree
    path — mirroring `get_tracking_branch`'s handling of the same access —
    instead of letting `TypeError` escape uncaught.
    """
    git_mock = _fake_git_repo(monkeypatch)
    # `active_branch` is a property on the real GitPython `Repo`; accessing it
    # itself raises when HEAD is detached (not `.name` afterwards), so the
    # attribute access on this mock instance must raise too.
    type(git_mock.Repo.return_value).active_branch = PropertyMock(
        side_effect=TypeError("HEAD is a detached symbolic reference as it points to 'abc123'")
    )

    with pytest.raises(RepoError) as ei:
        adapter.set_upstream_to(_REPO_PATH, "origin/main", repo_name="repo", env=None)

    assert str(_REPO_PATH) in str(ei.value)
    git_mock.Repo.return_value.git.config.assert_not_called()


def test_set_upstream_to_derives_message_from_value_error_instead_of_asserting_detached(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    """A `ValueError` from `active_branch` isn't always an unborn HEAD.

    A worktree orphaned by a deleted/re-cloned source checkout raises
    `ValueError: Reference at 'HEAD' does not exist` — a broken worktree, not a
    detached one. The wrapped message must carry the library's own text instead
    of asserting one diagnosis ("detached") for every `ValueError`.
    """
    git_mock = _fake_git_repo(monkeypatch)
    type(git_mock.Repo.return_value).active_branch = PropertyMock(
        side_effect=ValueError("Reference at 'HEAD' does not exist")
    )

    with pytest.raises(RepoError) as ei:
        adapter.set_upstream_to(_REPO_PATH, "origin/main", repo_name="repo", env=None)

    assert "Reference at 'HEAD' does not exist" in str(ei.value)
    assert "detached" not in str(ei.value)
    git_mock.Repo.return_value.git.config.assert_not_called()


# ── set_push_default_upstream ──────────────────────────────────────────────


def test_set_push_default_upstream_writes_config(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    cw = MagicMock()
    git_mock.Repo.return_value.config_writer.return_value.__enter__ = MagicMock(return_value=cw)
    git_mock.Repo.return_value.config_writer.return_value.__exit__ = MagicMock(return_value=False)

    adapter.set_push_default_upstream(_REPO_PATH, repo_name="repo", env=None)

    cw.set_value.assert_called_once_with("push", "default", "upstream")


# ── set_user_identity ──────────────────────────────────────────────────────


def test_set_user_identity_writes_name_and_email(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    cw = MagicMock()
    git_mock.Repo.return_value.config_writer.return_value.__enter__ = MagicMock(return_value=cw)
    git_mock.Repo.return_value.config_writer.return_value.__exit__ = MagicMock(return_value=False)

    adapter.set_user_identity(_REPO_PATH, name="Alice", email="alice@example.com", repo_name="repo", env=None)

    git_mock.Repo.assert_called_once_with(str(_REPO_PATH))
    assert call("user", "name", "Alice") in cw.set_value.call_args_list
    assert call("user", "email", "alice@example.com") in cw.set_value.call_args_list


# ── get_push_default ───────────────────────────────────────────────────────


def test_get_push_default_returns_value_when_set(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    cr = MagicMock()
    cr.get_value.return_value = "upstream"
    git_mock.Repo.return_value.config_reader.return_value.__enter__ = MagicMock(return_value=cr)
    git_mock.Repo.return_value.config_reader.return_value.__exit__ = MagicMock(return_value=False)

    result = adapter.get_push_default(_REPO_PATH, repo_name="repo", env=None)

    assert result == "upstream"
    # Must use config_reader (no lock), not config_writer.
    git_mock.Repo.return_value.config_reader.assert_called_once()
    git_mock.Repo.return_value.config_writer.assert_not_called()


def test_get_push_default_returns_none_when_empty(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    cr = MagicMock()
    cr.get_value.return_value = ""
    git_mock.Repo.return_value.config_reader.return_value.__enter__ = MagicMock(return_value=cr)
    git_mock.Repo.return_value.config_reader.return_value.__exit__ = MagicMock(return_value=False)

    result = adapter.get_push_default(_REPO_PATH, repo_name="repo", env=None)

    assert result is None


# ── is_worktree_clean ──────────────────────────────────────────────────────


def test_is_worktree_clean_returns_true_when_status_empty(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.status.return_value = ""

    result = adapter.is_worktree_clean(_REPO_PATH, repo_name="repo", env=None)

    assert result is True
    git_mock.Repo.return_value.git.status.assert_called_once_with("--porcelain")


def test_is_worktree_clean_returns_false_when_status_nonempty(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.status.return_value = " M README.md\n"

    result = adapter.is_worktree_clean(_REPO_PATH, repo_name="repo", env=None)

    assert result is False


def test_is_worktree_clean_returns_false_on_invalid_git_repository(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.side_effect = git.InvalidGitRepositoryError("not a git repo")

    result = adapter.is_worktree_clean(_REPO_PATH, repo_name="repo", env=None)

    assert result is False


def test_is_worktree_clean_returns_false_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.status.side_effect = git.GitCommandError(
        ("git", "status"), 128, stderr=b"not a git repo"
    )

    result = adapter.is_worktree_clean(_REPO_PATH, repo_name="repo", env=None)

    assert result is False


# ── resolve_ref ────────────────────────────────────────────────────────────


_FULL_SHA = "9f3c1ab2e4d5c6f7089a1b2c3d4e5f60718293a4"


def test_resolve_ref_branch_matches_remote_tracking_ref(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    # First candidate (refs/remotes/origin/<ref>) succeeds → branch.
    git_mock.Repo.return_value.git.rev_parse.return_value = _FULL_SHA

    kind, sha = adapter.resolve_ref(_REPO_PATH, "main", repo_name="repo", env=None)

    assert kind is RefKind.branch
    assert sha == _FULL_SHA
    git_mock.Repo.return_value.git.rev_parse.assert_called_once_with("--verify", "refs/remotes/origin/main")


def test_resolve_ref_tag_matches_on_second_candidate(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    # First candidate fails (not a remote branch), second (tag) succeeds.
    git_mock.Repo.return_value.git.rev_parse.side_effect = [
        git.GitCommandError(("git", "rev-parse", "--verify"), 128),
        _FULL_SHA,
    ]

    kind, sha = adapter.resolve_ref(_REPO_PATH, "v1.4.2", repo_name="repo", env=None)

    assert kind is RefKind.tag
    assert sha == _FULL_SHA
    assert git_mock.Repo.return_value.git.rev_parse.call_count == 2
    git_mock.Repo.return_value.git.rev_parse.assert_called_with("--verify", "refs/tags/v1.4.2")


def test_resolve_ref_commit_matches_on_third_candidate(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    # First two candidates fail; third (raw SHA) succeeds.
    git_mock.Repo.return_value.git.rev_parse.side_effect = [
        git.GitCommandError(("git", "rev-parse", "--verify"), 128),
        git.GitCommandError(("git", "rev-parse", "--verify"), 128),
        _FULL_SHA,
    ]

    kind, sha = adapter.resolve_ref(_REPO_PATH, _FULL_SHA[:8], repo_name="repo", env=None)

    assert kind is RefKind.commit
    assert sha == _FULL_SHA
    assert git_mock.Repo.return_value.git.rev_parse.call_count == 3
    git_mock.Repo.return_value.git.rev_parse.assert_called_with("--verify", f"{_FULL_SHA[:8]}^{{commit}}")


def test_resolve_ref_raises_repo_error_when_all_candidates_fail(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_parse.side_effect = git.GitCommandError(("git", "rev-parse", "--verify"), 128)

    with pytest.raises(RepoError) as ei:
        adapter.resolve_ref(_REPO_PATH, "nonexistent-ref", repo_name="repo", env=None)

    assert "unresolvable ref" in ei.value.message
    assert "nonexistent-ref" in ei.value.message
    assert git_mock.Repo.return_value.git.rev_parse.call_count == 3


# ── checkout_detached ──────────────────────────────────────────────────────


def test_checkout_detached_calls_checkout_with_detach_flag(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.checkout_detached(_REPO_PATH, _FULL_SHA, repo_name="repo", env=None)

    git_mock.Repo.return_value.git.checkout.assert_called_once_with("--detach", _FULL_SHA)


def test_checkout_detached_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.checkout.side_effect = git.GitCommandError(
        ("git", "checkout", "--detach"), 128, stderr=b"no such commit"
    )

    with pytest.raises(RepoError) as ei:
        adapter.checkout_detached(_REPO_PATH, "deadbeef", repo_name="repo", env=None)

    assert "checkout --detach" in ei.value.message
    assert ei.value.subcommand == "checkout"


# ── checkout_branch ────────────────────────────────────────────────────────


def test_checkout_branch_calls_checkout_with_track_flag(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)

    adapter.checkout_branch(_REPO_PATH, "feature", repo_name="repo", env=None)

    git_mock.Repo.return_value.git.checkout.assert_called_once_with("-B", "feature", "--track", "origin/feature")


def test_checkout_branch_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.checkout.side_effect = git.GitCommandError(
        ("git", "checkout", "-B"), 128, stderr=b"no such remote branch"
    )

    with pytest.raises(RepoError) as ei:
        adapter.checkout_branch(_REPO_PATH, "no-such-branch", repo_name="repo", env=None)

    assert "checkout -B" in ei.value.message
    assert ei.value.subcommand == "checkout"


# ── get_head_commit ────────────────────────────────────────────────────────


def test_get_head_commit_returns_full_sha(monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_parse.return_value = _FULL_SHA

    result = adapter.get_head_commit(_REPO_PATH, repo_name="repo", env=None)

    assert result == _FULL_SHA
    git_mock.Repo.return_value.git.rev_parse.assert_called_once_with("HEAD")


def test_get_head_commit_strips_trailing_whitespace(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_parse.return_value = _FULL_SHA + "\n"

    result = adapter.get_head_commit(_REPO_PATH, repo_name="repo", env=None)

    assert result == _FULL_SHA


def test_get_head_commit_raises_repo_error_on_git_command_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.return_value.git.rev_parse.side_effect = git.GitCommandError(
        ("git", "rev-parse", "HEAD"), 128, stderr=b"not a git repo"
    )

    with pytest.raises(RepoError) as ei:
        adapter.get_head_commit(_REPO_PATH, repo_name="repo", env=None)

    assert "rev-parse HEAD" in ei.value.message
    assert ei.value.subcommand == "rev-parse"


@pytest.mark.parametrize("exc", [git.InvalidGitRepositoryError("not a git repo"), git.NoSuchPathError("no such path")])
def test_get_head_commit_wraps_invalid_repo_and_missing_path_as_repo_error(
    monkeypatch: pytest.MonkeyPatch, adapter: GitPythonRepository, exc: Exception
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    git_mock.Repo.side_effect = exc

    with pytest.raises(RepoError, match="not a git repository") as raised:
        adapter.get_head_commit(_REPO_PATH, repo_name="repo", env=None)

    assert raised.value.cwd == str(_REPO_PATH)


# ── add_worktree (real-git regression: issue #148) ─────────────────────────
#
# These use a real `git init` repo rather than mocks so the `--no-track` fix
# is validated against actual git behavior — the incidental-upstream bug this
# guards against only reproduces under real git's `branch.autoSetupMerge` /
# remote-tracking-base semantics, which a mocked `git.worktree` call can't
# exercise.


def _configure(r: git.Repo) -> git.Repo:
    with r.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")
        cw.set_value("commit", "gpgsign", "false")
    return r


def test_add_worktree_no_track_survives_auto_setup_merge_always(tmp_path: Path) -> None:
    """Branch created from a local base is not born tracking that base under `autoSetupMerge = always`."""
    adapter = GitPythonRepository(RepoErrorFactory(), NoopCommandTracer())
    source = tmp_path / "source"
    r = _configure(git.Repo.init(str(source), initial_branch="master"))
    (source / "README").write_text("init\n")
    r.index.add(["README"])
    r.index.commit("init")
    with r.config_writer() as cw:
        cw.set_value("branch", "autoSetupMerge", "always")

    worktree_path = tmp_path / "env" / "source"
    adapter.add_worktree(source, worktree_path, branch="beta", base_branch="master", repo_name="repo", env=None)

    with git.Repo(str(worktree_path)) as wt:
        tb = wt.active_branch.tracking_branch()
    assert tb is None


def test_add_worktree_no_track_survives_remote_tracking_base(tmp_path: Path) -> None:
    """Branch created from a remote-tracking base ref does not inherit that ref as its upstream."""
    adapter = GitPythonRepository(RepoErrorFactory(), NoopCommandTracer())
    origin_bare = tmp_path / "origin.git"
    seed = _configure(git.Repo.init(str(tmp_path / "seed"), initial_branch="master"))
    (tmp_path / "seed" / "README").write_text("init\n")
    seed.index.add(["README"])
    seed.index.commit("init")
    seed.git.clone("--bare", str(tmp_path / "seed"), str(origin_bare))

    source = tmp_path / "source"
    _configure(git.Repo.clone_from(str(origin_bare), str(source)))

    worktree_path = tmp_path / "env" / "source"
    adapter.add_worktree(source, worktree_path, branch="gamma", base_branch="origin/master", repo_name="repo", env=None)

    with git.Repo(str(worktree_path)) as wt:
        tb = wt.active_branch.tracking_branch()
    assert tb is None


# ── set_upstream_to (real-git: detached HEAD) ───────────────────────────────
#
# A mocked `active_branch` can only simulate a `TypeError`; this drives a real
# `git checkout --detach` so the adapter is exercised against GitPython's
# actual detached-HEAD failure mode rather than a guess at its shape.


def test_set_upstream_to_raises_repo_error_on_genuinely_detached_head(tmp_path: Path) -> None:
    adapter = GitPythonRepository(RepoErrorFactory(), NoopCommandTracer())
    repo_path = tmp_path / "demo"
    r = _configure(git.Repo.init(str(repo_path), initial_branch="main"))
    (repo_path / "README").write_text("init\n")
    r.index.add(["README"])
    commit_sha = r.index.commit("init").hexsha
    r.git.checkout("--detach", commit_sha)

    with pytest.raises(RepoError) as ei:
        adapter.set_upstream_to(repo_path, "origin/main", repo_name="repo", env=None)

    assert str(repo_path) in str(ei.value)
    # No tracking config was written — the write never happens once HEAD can't
    # be resolved to a branch.
    with git.Repo(str(repo_path)) as check, check.config_reader() as cr:
        assert not cr.has_section("branch.main")
