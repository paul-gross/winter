from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest

from winter_cli.modules.workspace.internal import read_repo_repository
from winter_cli.modules.workspace.internal.read_repo_repository import (
    ReadRepoRepository,
    _parse_graph_log,
    _parse_main_ahead_behind,
    _parse_status_porcelain_v2,
)
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    DiffMode,
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    RepoError,
    StandaloneRepository,
    Workspace,
)

_ROOT = Path("/fake/workspace")
_PROJECT_PATH = _ROOT / "projects" / "demo"
_EXT_PATH = _ROOT / "ext"
_ALPHA_PATH = _ROOT / "alpha"
_WT_PATH = _ALPHA_PATH / "demo"

# Records are NUL-terminated by `git status --porcelain=v2 -z`; the helper joins
# the per-record fixtures the same way (trailing NUL included) so the parser sees
# real framing.
_NUL = "\x00"


def _porcelain(*records: str) -> str:
    return "".join(rec + _NUL for rec in records)


def _fake_git_repo(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    git_mock = MagicMock()
    git_mock.GitCommandError = git.GitCommandError
    git_mock.InvalidGitRepositoryError = git.InvalidGitRepositoryError
    git_mock.NoSuchPathError = git.NoSuchPathError
    git_mock.BadName = git.BadName
    # The implementation uses `with git.Repo(...) as r:`, so __enter__ must return
    # the same mock that tests assert against.
    git_mock.Repo.return_value.__enter__.return_value = git_mock.Repo.return_value
    monkeypatch.setattr(read_repo_repository, "git", git_mock)
    return git_mock


@pytest.fixture
def repo() -> ReadRepoRepository:
    return ReadRepoRepository(RepoErrorFactory())


def _worktree(name: str = "demo", branch: str = "alpha") -> FeatureWorktree:
    workspace = Workspace(root_path=_ROOT, session_prefix="t", main_branch="main")
    env = FeatureEnvironment(workspace=workspace, name=branch, index=1, path=_ALPHA_PATH)
    project = ProjectRepository(name=name, main_path=_ALPHA_PATH / name, main_branch="main")
    return FeatureWorktree(workspace=workspace, environment=env, repository=project)


# --------------------------------------------------------------------------- #
# Pure parser: `git status --porcelain=v2 --branch -z`
# --------------------------------------------------------------------------- #


def test_parse_status_branch_tracking_present() -> None:
    out = _porcelain(
        "# branch.oid abc123",
        "# branch.head alpha",
        "# branch.upstream origin/feature",
        "# branch.ab +3 -2",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.branch == "alpha"
    assert status.tracking_branch == "origin/feature"
    assert status.tracking_ahead == 3
    assert status.tracking_behind == 2
    assert status.tracking_ref_present is True


def test_parse_status_upstream_unfetched_has_no_ab() -> None:
    # An upstream configured but never fetched prints branch.upstream but omits
    # branch.ab — git can't compute ahead/behind against a missing ref.
    out = _porcelain(
        "# branch.oid abc123",
        "# branch.head alpha",
        "# branch.upstream origin/feature",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.tracking_branch == "origin/feature"
    assert status.tracking_ref_present is False
    assert status.tracking_ahead == 0
    assert status.tracking_behind == 0


def test_parse_status_no_upstream() -> None:
    out = _porcelain("# branch.oid abc123", "# branch.head alpha")
    status = _parse_status_porcelain_v2(out)
    assert status.tracking_branch is None
    assert status.tracking_ref_present is False


def test_parse_status_detached_head() -> None:
    out = _porcelain("# branch.oid abc123", "# branch.head (detached)")
    status = _parse_status_porcelain_v2(out)
    assert status.branch is None


def test_parse_status_staged_unstaged_untracked() -> None:
    out = _porcelain(
        "# branch.head alpha",
        "1 .M N... 100644 100644 100644 hHHHHHH hIIIIII unstaged.txt",
        "1 A. N... 000000 100644 100644 0000000 hIIIIII staged.txt",
        "1 MM N... 100644 100644 100644 hHHHHHH hIIIIII partial.txt",
        "? untracked.txt",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.staged_files == ["staged.txt", "partial.txt"]
    assert status.unstaged_files == ["unstaged.txt", "partial.txt"]
    assert status.untracked_files == ["untracked.txt"]
    # dirty_files is dedup(staged + unstaged) + untracked: staged paths first, the
    # partially-staged file deduped to a single entry, untracked appended last.
    assert status.dirty_files == ["staged.txt", "partial.txt", "unstaged.txt", "untracked.txt"]


def test_parse_status_rename_consumes_original_path() -> None:
    # A type-2 record carries the original path in the *next* record; the parser
    # must consume it so the following untracked entry isn't misread.
    out = _porcelain(
        "# branch.head alpha",
        "2 R. N... 100644 100644 100644 hHHHHHH hIIIIII R100 new.txt",
        "old.txt",
        "? untracked.txt",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.staged_files == ["new.txt"]
    assert status.unstaged_files == []
    assert status.untracked_files == ["untracked.txt"]


def test_parse_status_unmerged_counts_as_both() -> None:
    # Unmerged paths diverged in both index and worktree — GitPython listed them
    # in both diffs, so they land in staged and unstaged alike.
    out = _porcelain(
        "# branch.head alpha",
        "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.txt",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.staged_files == ["conflict.txt"]
    assert status.unstaged_files == ["conflict.txt"]
    assert status.dirty_files == ["conflict.txt"]


def test_parse_status_preserves_spaced_path() -> None:
    out = _porcelain(
        "# branch.head alpha",
        "1 A. N... 000000 100644 100644 0000000 hIIIIII spaced name.txt",
    )
    status = _parse_status_porcelain_v2(out)
    assert status.staged_files == ["spaced name.txt"]


# --------------------------------------------------------------------------- #
# Pure parser: `git rev-list --left-right --count`
# --------------------------------------------------------------------------- #


def test_parse_main_ahead_behind_orientation() -> None:
    # "<left>\t<right>": left = origin/<main>-only (behind), right = HEAD-only (ahead).
    ahead, behind = _parse_main_ahead_behind("2\t5\n")
    assert ahead == 5
    assert behind == 2


def test_parse_main_ahead_behind_zero() -> None:
    assert _parse_main_ahead_behind("0\t0\n") == (0, 0)


# --------------------------------------------------------------------------- #
# Pure parser: `git log --graph`
# --------------------------------------------------------------------------- #


# `_GRAPH_FORMAT` opens each commit's text with a \x00 sentinel: a line is
# "<glyphs>\x00<rendered>\x1f<full-hash>\x1f<subject>".
_SENTINEL = "\x00"
_SEP = "\x1f"


def _graph_line(glyphs: str, rendered: str, full_hash: str, subject: str) -> str:
    return f"{glyphs}{_SENTINEL}{rendered}{_SEP}{full_hash}{_SEP}{subject}"


def test_parse_graph_log_excludes_boundary() -> None:
    # `* <line>` are real commits; the `o <line>` boundary (merge-base from
    # --boundary) is rendered into the graph but excluded from recent_commits.
    out = "\n".join(
        [
            _graph_line("* ", "ed15297 (HEAD -> alpha) feat: second", "ed15297" + "a" * 33, "feat: second"),
            _graph_line("* ", "9a686d4 feat: first", "9a686d4" + "b" * 33, "feat: first"),
            _graph_line("o ", "7bf838a (origin/main) base", "7bf838a" + "c" * 33, "base"),
        ]
    )
    graph_lines, commits = _parse_graph_log(out)
    assert graph_lines == [
        "* ed15297 (HEAD -> alpha) feat: second",
        "* 9a686d4 feat: first",
        "o 7bf838a (origin/main) base",
    ]
    assert [c.short_hash for c in commits] == ["ed15297", "9a686d4"]
    assert [c.message for c in commits] == ["feat: second", "feat: first"]


def test_parse_graph_log_preserves_connector_lines() -> None:
    # Pure merge-connector lines carry no \x00 sentinel: they belong in the graph
    # verbatim but contribute no commit.
    out = "\n".join(
        [
            _graph_line("*   ", "e7cbc24 Merge branch", "e7cbc24" + "d" * 33, "Merge branch"),
            "|\\  ",
            _graph_line("| * ", "354e941 add c", "354e941" + "e" * 33, "add c"),
            _graph_line("* | ", "2970288 add b", "2970288" + "f" * 33, "add b"),
            "|/  ",
        ]
    )
    graph_lines, commits = _parse_graph_log(out)
    assert "|\\  " in graph_lines
    assert "|/  " in graph_lines
    assert [c.short_hash for c in commits] == ["e7cbc24", "354e941", "2970288"]


def test_parse_graph_log_boundary_detection_ignores_subject_text() -> None:
    # A subject containing `o` on a `* ` (non-boundary) line must NOT be mistaken
    # for a boundary — the glyph run is isolated by the sentinel, so the subject
    # text can't leak into boundary detection regardless of how short the hash is.
    out = "\n".join(
        [
            _graph_line("* ", "1911 merge of stuff into core", "1911" + "a" * 36, "merge of stuff into core"),
            _graph_line("o ", "3aaf base", "3aaf" + "c" * 36, "base"),
        ]
    )
    _, commits = _parse_graph_log(out)
    assert [c.message for c in commits] == ["merge of stuff into core"]


# --------------------------------------------------------------------------- #
# Call-count: the three-richer-calls contract (AC: ~3 git calls per repo)
# --------------------------------------------------------------------------- #


def test_build_repo_status_issues_three_git_calls(monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.git.status.return_value = _porcelain("# branch.head alpha")
    r.git.rev_list.return_value = "0\t0\n"
    r.git.log.return_value = ""
    project = ProjectRepository(name="demo", main_path=_PROJECT_PATH, main_branch="main")

    repo.get_project_status(project)

    # Exactly the three richer calls in the happy path — no narrow per-question
    # probes, no defensive rev-parse (that only fires on a tolerated failure).
    assert r.git.status.call_count == 1
    assert r.git.rev_list.call_count == 1
    assert r.git.log.call_count == 1
    assert r.git.rev_parse.call_count == 0


def test_build_repo_status_status_call_uses_porcelain_v2(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.git.status.return_value = _porcelain("# branch.head alpha")
    r.git.rev_list.return_value = "0\t0\n"
    r.git.log.return_value = ""
    project = ProjectRepository(name="demo", main_path=_PROJECT_PATH, main_branch="main")

    repo.get_project_status(project)

    r.git.status.assert_called_once_with("--porcelain=v2", "--branch", "--untracked-files=all", "-z")
    r.git.rev_list.assert_called_once_with("--left-right", "--count", "origin/main...HEAD")


def test_build_repo_status_raises_when_present_main_ref_rev_list_fails(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    # rev-list fails but origin/main resolves — a real error, not the tolerated
    # missing-ref case, so it propagates as a RepoError.
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.git.status.return_value = _porcelain("# branch.head alpha")
    r.git.rev_list.side_effect = git.GitCommandError("rev-list", 128)
    r.git.rev_parse.return_value = "deadbeef"  # main ref present
    project = ProjectRepository(name="demo", main_path=_PROJECT_PATH, main_branch="main")

    with pytest.raises(RepoError):
        repo.get_project_status(project)


def test_build_repo_status_raises_when_present_main_ref_log_fails(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    # The graph log fails but origin/main resolves — propagates rather than
    # silently falling back to the HEAD graph.
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.git.status.return_value = _porcelain("# branch.head alpha")
    r.git.rev_list.return_value = "0\t0\n"
    r.git.log.side_effect = git.GitCommandError("log", 128)
    r.git.rev_parse.return_value = "deadbeef"  # main ref present
    project = ProjectRepository(name="demo", main_path=_PROJECT_PATH, main_branch="main")

    with pytest.raises(RepoError):
        repo.get_project_status(project)


# --------------------------------------------------------------------------- #
# Missing-on-disk: empty status, no git calls
# --------------------------------------------------------------------------- #


def test_get_project_status_returns_empty_when_path_missing(repo: ReadRepoRepository) -> None:
    project = ProjectRepository(
        name="ghost",
        main_path=Path("/fake/does-not-exist/ghost"),
        main_branch="main",
    )
    status = repo.get_project_status(project)
    assert status.name == "ghost"
    assert status.branch is None
    assert status.dirty_files == []


def test_get_standalone_status_returns_empty_when_path_missing(repo: ReadRepoRepository) -> None:
    standalone = StandaloneRepository(name="ext", path=Path("/fake/does-not-exist/ext"))
    status = repo.get_standalone_status(standalone)
    assert status.name == "ext"
    assert status.branch is None


# --------------------------------------------------------------------------- #
# get_standalone_status (unchanged GitPython path) — still covered
# --------------------------------------------------------------------------- #


def test_get_standalone_status_reads_branch_and_commit(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.active_branch.name = "main"
    r.active_branch.tracking_branch.return_value = None
    r.index.diff.return_value = []
    r.untracked_files = []
    commit = MagicMock()
    commit.message = "init"
    r.head.commit = commit
    standalone = StandaloneRepository(name="ext", path=_EXT_PATH)

    status = repo.get_standalone_status(standalone)

    assert status.branch == "main"
    assert status.latest_commit == "init"


def test_get_standalone_status_counts_staged_files_as_dirty(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    r = git_mock.Repo.return_value
    r.active_branch.name = "main"
    r.active_branch.tracking_branch.return_value = None
    staged_item = MagicMock()
    staged_item.a_path = "staged.py"
    r.index.diff.side_effect = lambda arg: [staged_item] if arg == "HEAD" else []
    r.untracked_files = []
    commit = MagicMock()
    commit.message = "init"
    r.head.commit = commit
    standalone = StandaloneRepository(name="ext", path=_EXT_PATH)

    status = repo.get_standalone_status(standalone)

    assert status.dirty_count == 1


# --------------------------------------------------------------------------- #
# get_diff / get_workspace (unchanged) — still covered
# --------------------------------------------------------------------------- #


def test_get_diff_for_uncommitted_returns_text_and_stats(
    monkeypatch: pytest.MonkeyPatch, repo: ReadRepoRepository
) -> None:
    git_mock = _fake_git_repo(monkeypatch)
    r = git_mock.Repo.return_value
    diff_text = "diff --git a/demo/README.md b/demo/README.md\n--- a/demo/README.md\n+++ b/demo/README.md\n@@ -1 +1,2 @@\n+line\n"
    numstat_text = "1\t0\tREADME.md\n"
    r.git.diff.side_effect = [diff_text, numstat_text]
    wt = _worktree()

    result = repo.get_diff(wt, DiffMode.uncommitted)

    assert result.repo_name == "demo"
    assert result.diff_text == diff_text
    assert result.files_changed == 1


def test_get_workspace_constructs_domain_object(repo: ReadRepoRepository) -> None:
    workspace = repo.get_workspace(_ROOT, "test", "main")
    assert workspace.root_path == _ROOT
    assert workspace.session_prefix == "test"
    assert workspace.main_branch == "main"


# --------------------------------------------------------------------------- #
# End-to-end against real git repos — guards against format drift the mocks
# can't catch (porcelain framing, graph glyphs, rev-list orientation).
# --------------------------------------------------------------------------- #


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


def _commit(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)


def _project(path: Path, name: str = "demo", main_branch: str | None = "main") -> ProjectRepository:
    # main_branch=None models a repo with no configured main branch: get_project_status
    # then runs only the tracking probe (no origin/<main> ahead/behind).
    return ProjectRepository(name=name, main_path=path, main_branch=main_branch)


def test_real_clean_tree(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "a\n", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")

    status = repo.get_project_status(_project(tmp_path))

    assert status.branch == "main"
    assert status.dirty_files == []
    assert status.staged_count == 0
    assert status.unstaged_count == 0
    assert status.untracked_count == 0
    assert status.ahead == 0
    assert status.behind == 0


def test_real_dirty_files(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "tracked.txt", "a\n", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    # staged-only, unstaged-only, partially-staged, untracked (incl. nested dir)
    (tmp_path / "staged.txt").write_text("s\n")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "tracked.txt").write_text("a\nb\n")  # unstaged modification
    (tmp_path / "partial.txt").write_text("p\n")
    _git(tmp_path, "add", "partial.txt")
    (tmp_path / "partial.txt").write_text("p\nq\n")  # staged then re-modified
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "u.txt").write_text("u\n")

    status = repo.get_project_status(_project(tmp_path))

    assert status.staged_count == 2  # staged.txt + partial.txt
    assert status.unstaged_count == 2  # tracked.txt + partial.txt
    assert status.untracked_count == 1  # sub/u.txt (recursed, matching ls-files)
    assert "sub/u.txt" in status.dirty_files
    assert "staged.txt" in status.dirty_files
    assert "tracked.txt" in status.dirty_files
    # partially-staged file appears once despite being in both diffs
    assert status.dirty_files.count("partial.txt") == 1


def test_real_ahead_behind_against_origin_main(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "1\n", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    # origin/main advances by one (behind), HEAD advances by two (ahead).
    _commit(tmp_path, "f.txt", "2\n", "head-1")
    _commit(tmp_path, "f.txt", "3\n", "head-2")
    base = _git(tmp_path, "rev-parse", "HEAD~2").strip()
    moved = _git(tmp_path, "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "origin-moved").strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", moved)

    status = repo.get_project_status(_project(tmp_path))

    assert status.ahead == 2
    assert status.behind == 1


def test_real_tracking_ahead_behind(tmp_path: Path, repo: ReadRepoRepository) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "tester")
    _commit(work, "f.txt", "1\n", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD")
    _commit(work, "f.txt", "2\n", "ahead")

    # main_branch=None so only the tracking probe is exercised here.
    status = repo.get_project_status(_project(work, main_branch=None))

    assert status.tracking_branch is not None
    assert status.tracking_ref_present is True
    assert status.tracking_ahead == 1
    assert status.tracking_behind == 0


def test_real_unfetched_tracking_ref(tmp_path: Path, repo: ReadRepoRepository) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "tester")
    _commit(work, "f.txt", "1\n", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD")
    # Drop the local tracking ref to simulate a configured-but-unfetched upstream.
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")

    status = repo.get_project_status(_project(work, main_branch=None))

    assert status.tracking_branch is not None  # upstream still configured
    assert status.tracking_ref_present is False
    assert status.tracking_ahead == 0
    assert status.tracking_behind == 0


def test_real_missing_origin_main_tolerated(tmp_path: Path, repo: ReadRepoRepository) -> None:
    # No origin/main ref at all (fresh clone, no fetch): ahead/behind fall to 0/0,
    # the commit graph falls back to HEAD, and recent_commits stays empty.
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "1\n", "only commit")

    status = repo.get_project_status(_project(tmp_path))

    assert status.ahead == 0
    assert status.behind == 0
    assert status.recent_commits == []
    assert any("only commit" in line for line in status.commit_graph)


def test_real_commit_graph_and_recent_commits(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "1\n", "base commit")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _commit(tmp_path, "f.txt", "2\n", "feat: first")
    _commit(tmp_path, "f.txt", "3\n", "feat: second")

    status = repo.get_project_status(_project(tmp_path))

    # recent_commits is the divergence ahead of origin/main, newest first, and
    # excludes the boundary merge-base.
    assert [c.message for c in status.recent_commits] == ["feat: second", "feat: first"]
    assert all(len(c.short_hash) == 7 for c in status.recent_commits)
    # The graph renders the boundary commit with an `o` glyph; the branch commits
    # render with `*`.
    assert any(line.startswith("o ") and "base commit" in line for line in status.commit_graph)
    assert any(line.startswith("* ") and "feat: second" in line for line in status.commit_graph)


def test_real_merge_topology_graph_and_tip(tmp_path: Path, repo: ReadRepoRepository) -> None:
    # A merge in the divergence: the graph must render full topology (merge node,
    # connector lines, boundary `o`), and the recent_commits tip is the HEAD
    # commit — the only recent_commits element read downstream.
    _init_repo(tmp_path)
    _commit(tmp_path, "a", "base\n", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    _commit(tmp_path, "b", "x\n", "add b")
    _git(tmp_path, "checkout", "-q", "-b", "side", "main")
    _commit(tmp_path, "c", "y\n", "add c on side")
    _git(tmp_path, "checkout", "-q", "feat")
    _git(tmp_path, "merge", "-q", "--no-edit", "side")
    _commit(tmp_path, "d", "z\n", "after merge")

    status = repo.get_project_status(_project(tmp_path))

    # Tip subject is HEAD, deterministic regardless of merge-sibling ordering.
    assert status.recent_commits[0].message == "after merge"
    # All four branch commits surface (set, not order); the boundary base does not.
    assert {c.message for c in status.recent_commits} == {
        "after merge",
        "add c on side",
        "add b",
        "Merge branch 'side' into feat",
    }
    # Full topology rendered: a merge node, a connector line, and the `o` boundary.
    assert any(line.startswith("|") for line in status.commit_graph)
    assert any(line.startswith("o ") and "base" in line for line in status.commit_graph)


def test_real_low_core_abbrev_keeps_all_recent_commits(tmp_path: Path, repo: ReadRepoRepository) -> None:
    # With core.abbrev below 7 the rendered `%h` is shorter than 7 chars; boundary
    # detection must still work and must not drop commits whose subject contains
    # `o`. Regression guard for the hash-width-independent glyph parse.
    _init_repo(tmp_path)
    _git(tmp_path, "config", "core.abbrev", "4")
    _commit(tmp_path, "f", "1\n", "base commit")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _commit(tmp_path, "f", "2\n", "merge of stuff into core")
    _commit(tmp_path, "f", "3\n", "second wOrk")

    status = repo.get_project_status(_project(tmp_path))

    assert [c.message for c in status.recent_commits] == ["second wOrk", "merge of stuff into core"]
    # The boundary base commit is still excluded.
    assert "base commit" not in [c.message for c in status.recent_commits]


def test_real_standalone_detail_lists_head_commits(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    for i in range(3):
        _commit(tmp_path, "f.txt", f"{i}\n", f"commit {i}")
    standalone = StandaloneRepository(name="ext", path=tmp_path)  # main_branch=None

    status = repo.get_standalone_detail(standalone)

    assert status.name == "ext"
    assert status.branch == "main"
    # recent_from_head lists tip commits on HEAD itself, newest first.
    assert [c.message for c in status.recent_commits] == ["commit 2", "commit 1", "commit 0"]
    assert any("commit 2" in line for line in status.commit_graph)


def test_real_detached_head(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "f.txt", "1\n", "init")
    _git(tmp_path, "checkout", "-q", "--detach")

    status = repo.get_project_status(_project(tmp_path, main_branch=None))

    assert status.branch is None
    assert status.tracking_branch is None


def test_real_unborn_head(tmp_path: Path, repo: ReadRepoRepository) -> None:
    # Brand-new repo with no commits: branch resolves, but there is no history to
    # graph and nothing dirty.
    _init_repo(tmp_path)

    status = repo.get_project_status(_project(tmp_path))

    assert status.branch == "main"
    assert status.dirty_files == []
    assert status.recent_commits == []
    assert status.commit_graph == []


def test_real_worktree_status_reports_branch(tmp_path: Path, repo: ReadRepoRepository) -> None:
    _init_repo(tmp_path, default_branch="alpha")
    _commit(tmp_path, "f.txt", "1\n", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    workspace = Workspace(root_path=tmp_path.parent, session_prefix="t", main_branch="main")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=tmp_path.parent)
    project = ProjectRepository(name=tmp_path.name, main_path=tmp_path, main_branch="main")
    wt = FeatureWorktree(workspace=workspace, environment=env, repository=project)

    status = repo.get_worktree_status(wt)

    assert status.branch == "alpha"
