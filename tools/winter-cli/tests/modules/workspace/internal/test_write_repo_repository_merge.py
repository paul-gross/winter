"""Real-git integration tests for `WriteRepoRepository.merge_ref`.

Mocking GitPython's merge / abort / unmerged-blob plumbing would just test
the mock; these tests build actual repos in `tmp_path` and exercise the
real `git merge` paths so the abort-on-conflict behavior, mode dispatch,
and missing-ref handling are observed end-to-end.
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
    MergeMode,
    MergeResult,
    ProjectRepository,
    StandaloneRepository,
    Workspace,
)


@pytest.fixture
def repo_svc() -> WriteRepoRepository:
    error_factory = RepoErrorFactory()
    git_ops = GitOpsService(error_factory, sleep=lambda _: None, jitter=lambda: 0.0)
    return WriteRepoRepository(error_factory=error_factory, git_ops=git_ops)


def _init_repo(path: Path) -> git.Repo:
    """Create a fresh repo at `path` with one initial commit on `main`."""
    path.mkdir(parents=True, exist_ok=True)
    r = git.Repo.init(str(path), initial_branch="main")
    with r.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")
        cw.set_value("commit", "gpgsign", "false")
    (path / "README").write_text("initial\n")
    r.index.add(["README"])
    r.index.commit("initial")
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


def _wt_from_repo(path: Path, name: str = "demo") -> FeatureWorktree:
    workspace = Workspace(root_path=path.parent, session_prefix="t", main_branch="main")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=path.parent)
    project_repo = ProjectRepository(name=name, main_path=path, main_branch="main")
    return FeatureWorktree(workspace=workspace, environment=env, repository=project_repo)


# --- ff_only mode --------------------------------------------------------------


def test_merge_ref_ff_only_fast_forwards_when_source_is_ahead(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """Default mode (ff_only): source branch ahead of HEAD, no divergence → fast-forward."""
    r = _init_repo(tmp_path / "demo")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature work\n", "add feature")
    r.git.checkout("main")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.fast_forwarded
    assert (tmp_path / "demo" / "feature.txt").exists()


def test_merge_ref_ff_only_up_to_date_when_source_already_reachable(
    tmp_path: Path, repo_svc: WriteRepoRepository
) -> None:
    """Source ref already in HEAD's history → up_to_date no-op."""
    r = _init_repo(tmp_path / "demo")
    r.git.tag("v1")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "v1", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.up_to_date


def test_merge_ref_ff_only_diverged_when_branches_have_split(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """ff_only refuses divergence and reports ahead/behind — no merge attempt."""
    r = _init_repo(tmp_path / "demo")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature\n", "add feature")
    r.git.checkout("main")
    _commit(r, "main.txt", "main\n", "add main")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.diverged
    assert outcome.ahead == 1
    assert outcome.behind == 1


# --- merge mode (--merge: 3-way fallback) -------------------------------------


def test_merge_ref_merge_mode_creates_merge_commit_when_diverged(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """--merge: ff when possible, 3-way merge commit when histories diverge."""
    r = _init_repo(tmp_path / "demo")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature\n", "add feature")
    r.git.checkout("main")
    _commit(r, "main.txt", "main\n", "add main")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.merge, autostash=False)

    assert outcome.result == MergeResult.merged
    assert len(r.head.commit.parents) == 2


def test_merge_ref_merge_mode_aborts_on_conflict_reports_diverged(
    tmp_path: Path, repo_svc: WriteRepoRepository
) -> None:
    """Conflict on --merge fallback: abort and report diverged. No MERGE_HEAD left behind."""
    r = _init_repo(tmp_path / "demo")
    _commit(r, "shared.txt", "common\nline\n", "common")
    r.git.checkout("-b", "feature")
    _commit(r, "shared.txt", "feature edit\nline\n", "feature edit")
    r.git.checkout("main")
    _commit(r, "shared.txt", "main edit\nline\n", "main edit")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.merge, autostash=False)

    assert outcome.result == MergeResult.diverged
    assert outcome.ahead >= 1
    assert outcome.behind >= 1
    # No in-progress merge — the abort cleaned up.
    assert not (_working_dir(r) / ".git" / "MERGE_HEAD").exists()


# --- no_ff mode (--no-ff: force merge commit) ---------------------------------


def test_merge_ref_no_ff_forces_merge_commit_on_ff_eligible(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """--no-ff: even when fast-forward is possible, create a merge commit."""
    r = _init_repo(tmp_path / "demo")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature\n", "add feature")
    r.git.checkout("main")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.no_ff, autostash=False)

    assert outcome.result == MergeResult.merged
    assert len(r.head.commit.parents) == 2


def test_merge_ref_no_ff_up_to_date_when_already_reachable(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """--no-ff against a source already reachable from HEAD: up_to_date, not an error."""
    r = _init_repo(tmp_path / "demo")
    r.git.tag("v1")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "v1", MergeMode.no_ff, autostash=False)

    assert outcome.result == MergeResult.up_to_date


def test_merge_ref_no_ff_up_to_date_when_local_is_ahead_of_source(
    tmp_path: Path, repo_svc: WriteRepoRepository
) -> None:
    """--no-ff when local has commits past source but source is fully in history.

    Regression for a bug where the short-circuit only fired when both
    ahead==0 and behind==0. With local ahead by 1 and behind by 0, git
    silently says "Already up to date" and exits 0, but the old code
    claimed `merged` without an actual merge commit being created.
    Behavior must be: report up_to_date and leave HEAD untouched.
    """
    r = _init_repo(tmp_path / "demo")
    r.git.tag("v1")  # source ref pinned at the initial commit
    _commit(r, "ahead.txt", "ahead of v1\n", "advance past v1")
    head_before = r.head.commit.hexsha
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "v1", MergeMode.no_ff, autostash=False)

    assert outcome.result == MergeResult.up_to_date
    assert r.head.commit.hexsha == head_before
    assert len(r.head.commit.parents) == 1


def test_merge_ref_no_ff_aborts_on_conflict_reports_diverged(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """Conflict on --no-ff: abort + diverged, same shape as --merge."""
    r = _init_repo(tmp_path / "demo")
    _commit(r, "shared.txt", "common\n", "common")
    r.git.checkout("-b", "feature")
    _commit(r, "shared.txt", "feature\n", "feature edit")
    r.git.checkout("main")
    _commit(r, "shared.txt", "main\n", "main edit")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.no_ff, autostash=False)

    assert outcome.result == MergeResult.diverged
    assert not (_working_dir(r) / ".git" / "MERGE_HEAD").exists()


# --- autostash ----------------------------------------------------------------


def test_merge_ref_autostash_succeeds_with_dirty_unrelated_files(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """--autostash: dirty unrelated files don't block the merge; restored after."""
    r = _init_repo(tmp_path / "demo")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature\n", "add feature")
    r.git.checkout("main")
    (tmp_path / "demo" / "scratch.txt").write_text("WIP\n")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "feature", MergeMode.ff_only, autostash=True)

    assert outcome.result == MergeResult.fast_forwarded
    assert (tmp_path / "demo" / "scratch.txt").read_text() == "WIP\n"


# --- missing source ref -------------------------------------------------------


def test_merge_ref_skipped_when_source_ref_missing(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """Source ref doesn't resolve locally → skipped_missing_ref with descriptive error."""
    _init_repo(tmp_path / "demo")
    wt = _wt_from_repo(tmp_path / "demo")

    outcome = repo_svc.merge_ref(wt, "does-not-exist", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.skipped_missing_ref
    assert outcome.error is not None
    assert "does-not-exist" in outcome.error


# --- standalone counterpart ---------------------------------------------------


def test_merge_ref_standalone_fast_forwards(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """Standalone repos go through the same merge paths — fast-forward case."""
    r = _init_repo(tmp_path / "stand")
    r.git.checkout("-b", "feature")
    _commit(r, "feature.txt", "feature\n", "add feature")
    r.git.checkout("main")
    standalone = StandaloneRepository(name="stand", path=tmp_path / "stand")

    outcome = repo_svc.merge_ref_standalone(standalone, "feature", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.fast_forwarded


def test_merge_ref_standalone_skipped_when_source_ref_missing(tmp_path: Path, repo_svc: WriteRepoRepository) -> None:
    """Standalone merge: missing source ref also surfaces as skipped."""
    _init_repo(tmp_path / "stand")
    standalone = StandaloneRepository(name="stand", path=tmp_path / "stand")

    outcome = repo_svc.merge_ref_standalone(standalone, "nope", MergeMode.ff_only, autostash=False)

    assert outcome.result == MergeResult.skipped_missing_ref
