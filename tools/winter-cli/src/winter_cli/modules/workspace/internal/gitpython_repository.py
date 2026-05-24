from __future__ import annotations

from pathlib import Path

import git

from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory


class GitPythonRepository:
    """GitPython-backed adapter for IGitRepository. Confines `git.*` usage to this file.

    Every method wraps `git.GitCommandError` / `git.InvalidGitRepositoryError` /
    `git.NoSuchPathError` via `RepoErrorFactory.from_git` so callers see only
    the winter-defined `RepoError`.
    """

    def __init__(self, error_factory: RepoErrorFactory) -> None:
        self._error_factory = error_factory

    # ── Cloning + worktrees ───────────────────────────────────────────────

    def clone(self, url: str, dest: Path) -> None:
        try:
            git.Repo.clone_from(url, str(dest))
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"clone failed for {url}",
                cwd=dest.parent,
            ) from exc

    def add_worktree(
        self,
        source: Path,
        worktree_path: Path,
        branch: str,
        base_branch: str | None = None,
    ) -> None:
        try:
            r = git.Repo(str(source))
            if base_branch is None:
                r.git.worktree("add", str(worktree_path), branch)
            else:
                r.git.worktree("add", str(worktree_path), "-b", branch, base_branch)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git worktree add failed at {worktree_path}",
                cwd=source,
            ) from exc

    def remove_worktree(self, source: Path, worktree_path: Path, force: bool) -> None:
        try:
            r = git.Repo(str(source))
            args = ["remove"]
            if force:
                args.append("--force")
            args.append(str(worktree_path))
            r.git.worktree(*args)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git worktree remove failed at {worktree_path}",
                cwd=source,
            ) from exc

    def list_worktrees(self, source: Path) -> list[Path]:
        try:
            r = git.Repo(str(source))
            output = r.git.worktree("list", "--porcelain")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git worktree list failed at {source}",
                cwd=source,
            ) from exc
        paths: list[Path] = []
        for line in output.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :]))
        return paths

    # ── Branches + tracking ──────────────────────────────────────────────

    def get_local_branches(self, path: Path) -> list[str]:
        r = git.Repo(str(path))
        return [h.name for h in r.heads]

    def get_tracking_branch(self, path: Path) -> str | None:
        r = git.Repo(str(path))
        try:
            tb = r.active_branch.tracking_branch()
        except (TypeError, ValueError):
            return None
        return tb.name if tb is not None else None

    def set_upstream_to(self, path: Path, ref: str) -> None:
        try:
            r = git.Repo(str(path))
            r.git.branch("--set-upstream-to", ref)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"set-upstream-to {ref} failed at {path}",
                cwd=path,
            ) from exc

    def set_push_default_upstream(self, path: Path) -> None:
        r = git.Repo(str(path))
        with r.config_writer() as cw:
            cw.set_value("push", "default", "upstream")

    # ── Repository-scope config ──────────────────────────────────────────

    def set_user_identity(self, path: Path, name: str, email: str) -> None:
        r = git.Repo(str(path))
        with r.config_writer(config_level="repository") as cw:
            cw.set_value("user", "name", name)
            cw.set_value("user", "email", email)

    def get_push_default(self, path: Path) -> str | None:
        r = git.Repo(str(path))
        with r.config_writer() as cw:
            value = cw.get_value("push", "default", "")
        return str(value) if value != "" else None

    # ── Status probes ────────────────────────────────────────────────────

    def is_worktree_clean(self, path: Path) -> bool:
        """True iff `git status --porcelain` reports no changes.

        Any failure (missing repo, git error) returns False so safety-check
        callers (destroy, prune) treat ambiguity as "do not touch".
        """
        try:
            r = git.Repo(str(path))
            output = r.git.status("--porcelain")
        except (git.InvalidGitRepositoryError, git.NoSuchPathError, git.GitCommandError):
            return False
        return not output.strip()


def _conforms_gitpython_repository(x: GitPythonRepository) -> IGitRepository:
    return x
