from __future__ import annotations

import logging
from pathlib import Path

import git

from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    DiffMode,
    FeatureWorktree,
    ProjectRepository,
    RepoCommit,
    RepoDiffResult,
    RepoStatus,
    StandaloneRepository,
    StandaloneRepoStatus,
    Workspace,
)
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository

logger = logging.getLogger(__name__)


class ReadRepoRepository:
    """Read-only GitPython implementation. All GitPython usage is confined here."""

    def __init__(self, error_factory: RepoErrorFactory) -> None:
        self._error_factory = error_factory

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        return self._build_repo_status(worktree.path, worktree.repository.name, worktree.repository.main_branch)

    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        # Missing-on-disk / not-a-repo aren't errors — the dashboard renders
        # the row as "not present" and the user knows to run init.
        if not repo.path.exists():
            return StandaloneRepoStatus(repository=repo)

        try:
            r = git.Repo(str(repo.path))
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return StandaloneRepoStatus(repository=repo)

        # TypeError on detached HEAD, ValueError on unborn HEAD — both are
        # legitimate "no active branch" states from GitPython's API.
        try:
            branch = r.active_branch.name
        except (TypeError, ValueError):
            branch = None

        try:
            dirty_count = len(r.index.diff(None)) + len(r.untracked_files)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"dirty-count probe failed for {repo.name}",
                cwd=repo.path,
            ) from exc

        ahead = 0
        behind = 0
        try:
            tb = r.active_branch.tracking_branch()
        except (TypeError, ValueError):
            tb = None
        if tb is not None:
            try:
                ahead = int(r.git.rev_list("--count", f"{tb.name}..HEAD"))
                behind = int(r.git.rev_list("--count", f"HEAD..{tb.name}"))
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"tracking ahead/behind probe failed for {repo.name}",
                    cwd=repo.path,
                ) from exc

        # ValueError / IndexError protect against unborn HEAD and empty messages;
        # both are legitimate "no commit yet" states, not git errors.
        latest_commit: str | None = None
        try:
            head_commit = r.head.commit
            # GitPython types `Commit.message` as `bytes | str`; decode the bytes case.
            message = head_commit.message
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            latest_commit = message.strip().splitlines()[0]
        except (ValueError, IndexError):
            pass

        return StandaloneRepoStatus(
            repository=repo,
            branch=branch,
            ahead=ahead,
            behind=behind,
            dirty_count=dirty_count,
            tracking_ahead=0,
            latest_commit=latest_commit,
        )

    def get_project_status(self, repo: ProjectRepository) -> RepoStatus:
        return self._build_repo_status(repo.main_path, repo.name, repo.main_branch)

    def get_diff(self, worktree: FeatureWorktree, mode: DiffMode) -> RepoDiffResult:
        name = worktree.repository.name
        r = git.Repo(str(worktree.path))

        src_prefix = f"--src-prefix=a/{name}/"
        dst_prefix = f"--dst-prefix=b/{name}/"

        try:
            if mode == DiffMode.uncommitted:
                diff_text = r.git.diff(src_prefix, dst_prefix)
                numstat = r.git.diff("--numstat")
            elif mode == DiffMode.staged:
                diff_text = r.git.diff("--staged", src_prefix, dst_prefix)
                numstat = r.git.diff("--staged", "--numstat")
            else:
                main_branch = worktree.repository.main_branch
                ref = f"origin/{main_branch}...HEAD"
                diff_text = r.git.diff(ref, src_prefix, dst_prefix)
                numstat = r.git.diff(ref, "--numstat")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"diff failed for {name}",
                cwd=worktree.path,
            ) from exc

        files_changed = 0
        insertions = 0
        deletions = 0
        for line in numstat.splitlines():
            if not line:
                continue
            parts = line.split("\t", 2)
            added = int(parts[0]) if parts[0] != "-" else 0
            removed = int(parts[1]) if parts[1] != "-" else 0
            insertions += added
            deletions += removed
            files_changed += 1

        ahead = 0
        if mode == DiffMode.branch:
            main_branch = worktree.repository.main_branch
            main_ref = f"origin/{main_branch}"
            try:
                ahead = int(r.git.rev_list("--count", f"{main_ref}..HEAD"))
            except git.GitCommandError as exc:
                # Same missing-ref tolerance as _build_repo_status: a brand-new
                # clone with no `origin/<main>` yet returns 0 commits ahead.
                try:
                    r.git.rev_parse("--verify", "--quiet", main_ref)
                except git.GitCommandError:
                    pass
                else:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"branch ahead probe failed for {name}",
                        cwd=worktree.path,
                    ) from exc

        return RepoDiffResult(
            repo_name=name,
            diff_text=diff_text,
            ahead=ahead,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
        )

    def get_workspace(self, root_path: Path, session_prefix: str, main_branch: str) -> Workspace:
        return Workspace(root_path=root_path, session_prefix=session_prefix, main_branch=main_branch)

    def _build_repo_status(
        self,
        repo_path: Path,
        name: str,
        main_branch: str | None,
    ) -> RepoStatus:
        # Missing-on-disk / not-a-repo aren't errors — they're legitimate
        # "this worktree hasn't been provisioned yet" states the dashboard
        # renders as an empty row.
        if not repo_path.exists():
            return RepoStatus(name=name, path=str(repo_path), main_branch=main_branch)

        try:
            r = git.Repo(str(repo_path))
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return RepoStatus(name=name, path=str(repo_path), main_branch=main_branch)

        # TypeError on detached HEAD, ValueError on unborn HEAD — both are
        # legitimate "no active branch" states from GitPython's API.
        try:
            branch = r.active_branch.name
        except (TypeError, ValueError):
            branch = None

        tracking_branch: str | None = None
        tracking_ahead = 0
        tracking_behind = 0
        tracking_ref_present = False
        try:
            tb = r.active_branch.tracking_branch()
        except (TypeError, ValueError):
            tb = None
        if tb is not None:
            tracking_branch = tb.name
            # `rev-list <ref>..HEAD` silently returns 0 when <ref> doesn't
            # resolve, so we can't tell "up-to-date" from "remote ref
            # missing" by tracking_ahead alone — verify the ref explicitly.
            try:
                r.git.rev_parse("--verify", "--quiet", tb.name)
                tracking_ref_present = True
            except git.GitCommandError:
                # Expected when the tracking ref hasn't been fetched yet
                # (freshly-connected feature branch). Not an error.
                pass
            if tracking_ref_present:
                try:
                    tracking_ahead = int(r.git.rev_list("--count", f"{tb.name}..HEAD"))
                    tracking_behind = int(r.git.rev_list("--count", f"HEAD..{tb.name}"))
                except git.GitCommandError as exc:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"tracking ahead/behind probe failed for {name}",
                        cwd=repo_path,
                    ) from exc

        ahead = 0
        behind = 0
        if main_branch:
            main_ref = f"origin/{main_branch}"
            try:
                ahead = int(r.git.rev_list("--count", f"{main_ref}..HEAD"))
                behind = int(r.git.rev_list("--count", f"HEAD..{main_ref}"))
            except git.GitCommandError as exc:
                # `rev-list` against a missing main ref returns non-zero on
                # some git versions; verify the ref explicitly so a missing
                # `origin/<main>` (e.g. a brand-new clone with no fetch yet)
                # falls through to 0/0 instead of poisoning every refresh.
                try:
                    r.git.rev_parse("--verify", "--quiet", main_ref)
                except git.GitCommandError:
                    pass
                else:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"main-branch ahead/behind probe failed for {name}",
                        cwd=repo_path,
                    ) from exc

        try:
            # GitPython types `Diff.a_path` as `str | None`; drop the None case to keep `list[str]`.
            dirty_files: list[str] = [item.a_path for item in r.index.diff(None) if item.a_path is not None]
            dirty_files += r.untracked_files
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"dirty-files probe failed for {name}",
                cwd=repo_path,
            ) from exc

        recent_commits: list[RepoCommit] = []
        if main_branch:
            try:
                for c in r.iter_commits(f"origin/{main_branch}..HEAD", max_count=5):
                    # GitPython types `Commit.message` as `bytes | str`; decode the bytes case.
                    msg = c.message
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", errors="replace")
                    recent_commits.append(RepoCommit(short_hash=c.hexsha[:7], message=msg.strip().splitlines()[0]))
            except git.GitCommandError as exc:
                # `iter_commits` against a missing `origin/<main>` is the same
                # case as the ahead/behind probe above — tolerate it.
                try:
                    r.git.rev_parse("--verify", "--quiet", f"origin/{main_branch}")
                except git.GitCommandError:
                    pass
                else:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"recent-commits probe failed for {name}",
                        cwd=repo_path,
                    ) from exc

        return RepoStatus(
            name=name,
            path=str(repo_path),
            main_branch=main_branch,
            branch=branch,
            ahead=ahead,
            behind=behind,
            dirty_files=dirty_files,
            recent_commits=recent_commits,
            tracking_branch=tracking_branch,
            tracking_ahead=tracking_ahead,
            tracking_behind=tracking_behind,
            tracking_ref_present=tracking_ref_present,
        )


# Typecheck-time conformance sentinel — Pyright rejects this return if
# ReadRepoRepository drifts from IReadRepoRepository. See
# winter-harness:/python/repository-pattern.md.
def _conforms_read_repo_repository(x: ReadRepoRepository) -> IReadRepoRepository:
    return x
