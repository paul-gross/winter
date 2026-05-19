from __future__ import annotations

import logging
from pathlib import Path

import git

from winter_cli.modules.workspace.models import (
    RepoCommit,
    DiffMode,
    ProjectRepository,
    RepoDiffResult,
    RepoStatus,
    StandaloneRepoStatus,
    StandaloneRepository,
    FeatureWorktree,
    Workspace,
)

logger = logging.getLogger(__name__)


class ReadRepoRepository:
    """Read-only GitPython implementation. All GitPython usage is confined here."""

    def get_worktree_status(self, worktree: FeatureWorktree) -> RepoStatus:
        return self._build_repo_status(worktree.path, worktree.repository.name, worktree.repository.main_branch)

    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        if not repo.path.exists():
            return StandaloneRepoStatus(repository=repo)

        try:
            r = git.Repo(str(repo.path))
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return StandaloneRepoStatus(repository=repo)

        try:
            branch = r.active_branch.name
        except TypeError:
            branch = None

        ahead = 0
        behind = 0
        dirty_count = 0
        try:
            dirty_count = len(r.index.diff(None)) + len(r.untracked_files)
        except git.GitCommandError:
            pass

        tracking_ahead = 0
        try:
            tb = r.active_branch.tracking_branch()
            if tb:
                ahead = int(r.git.rev_list("--count", f"{tb.name}..HEAD"))
                behind = int(r.git.rev_list("--count", f"HEAD..{tb.name}"))
        except (TypeError, git.GitCommandError):
            pass

        latest_commit: str | None = None
        try:
            head_commit = r.head.commit
            latest_commit = head_commit.message.strip().splitlines()[0]
        except (ValueError, IndexError):
            pass

        return StandaloneRepoStatus(
            repository=repo,
            branch=branch,
            ahead=ahead,
            behind=behind,
            dirty_count=dirty_count,
            tracking_ahead=tracking_ahead,
            latest_commit=latest_commit,
        )

    def get_project_status(self, repo: ProjectRepository) -> RepoStatus:
        return self._build_repo_status(repo.main_path, repo.name, repo.main_branch)

    def get_diff(self, worktree: FeatureWorktree, mode: DiffMode) -> RepoDiffResult:
        name = worktree.repository.name
        r = git.Repo(str(worktree.path))

        src_prefix = f"--src-prefix=a/{name}/"
        dst_prefix = f"--dst-prefix=b/{name}/"

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
            try:
                ahead = int(r.git.rev_list("--count", f"origin/{main_branch}..HEAD"))
            except git.GitCommandError:
                pass

        return RepoDiffResult(
            repo_name=name,
            diff_text=diff_text,
            ahead=ahead,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
        )

    def get_workspace(self, root_path: Path, session_prefix: str, main_branch_name: str) -> Workspace:
        return Workspace(root_path=root_path, session_prefix=session_prefix, main_branch=main_branch_name)

    def _build_repo_status(
        self,
        repo_path: Path,
        name: str,
        main_branch: str | None,
    ) -> RepoStatus:
        if not repo_path.exists():
            return RepoStatus(name=name, path=str(repo_path), main_branch=main_branch)

        try:
            r = git.Repo(str(repo_path))
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return RepoStatus(name=name, path=str(repo_path), main_branch=main_branch)

        try:
            branch = r.active_branch.name
        except TypeError:
            branch = None

        tracking_branch: str | None = None
        tracking_ahead = 0
        tracking_ref_present = False
        try:
            tb = r.active_branch.tracking_branch()
            if tb:
                tracking_branch = tb.name
                # `rev-list <ref>..HEAD` silently returns 0 when <ref> doesn't
                # resolve, so we can't tell "up-to-date" from "remote ref
                # missing" by tracking_ahead alone — verify the ref explicitly.
                try:
                    r.git.rev_parse("--verify", "--quiet", tb.name)
                    tracking_ref_present = True
                except git.GitCommandError:
                    pass
                if tracking_ref_present:
                    try:
                        tracking_ahead = int(r.git.rev_list("--count", f"{tb.name}..HEAD"))
                    except git.GitCommandError:
                        pass
        except TypeError:
            pass

        ahead = 0
        behind = 0
        if main_branch:
            main_ref = f"origin/{main_branch}"
            try:
                ahead = int(r.git.rev_list("--count", f"{main_ref}..HEAD"))
                behind = int(r.git.rev_list("--count", f"HEAD..{main_ref}"))
            except git.GitCommandError:
                pass

        dirty_files: list[str] = []
        try:
            dirty_files = [item.a_path for item in r.index.diff(None)]
            dirty_files += r.untracked_files
        except git.GitCommandError as e:
            logger.warning("Could not read dirty files for %s: %s", repo_path, e)

        recent_commits: list[RepoCommit] = []
        if main_branch:
            try:
                for c in r.iter_commits(f"origin/{main_branch}..HEAD", max_count=5):
                    recent_commits.append(
                        RepoCommit(short_hash=c.hexsha[:7], message=c.message.strip().splitlines()[0])
                    )
            except git.GitCommandError:
                pass

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
        )
