from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

import git

from winter_cli.core.tracing import IOperationTracer
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.internal.git_operation import GitOperationDeclaration, GitOperationExemption
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.models.domain_model import RefKind


class GitPythonRepository:
    """GitPython-backed adapter for IGitRepository. Confines `git.*` usage to this file.

    Every method wraps `git.GitCommandError` / `git.InvalidGitRepositoryError` /
    `git.NoSuchPathError` via `RepoErrorFactory.from_git` so callers see only
    the winter-defined `RepoError`.

    `clone_env` is layered over the inherited environment for `clone` — the
    one network operation that can prompt for credentials — so an in-process
    caller can pass `GIT_TERMINAL_PROMPT=0` to make a prompt fail fast.
    """

    def __init__(
        self,
        error_factory: RepoErrorFactory,
        tracer: IOperationTracer,
        clone_env: Mapping[str, str] | None = None,
    ) -> None:
        self._error_factory = error_factory
        self._tracer = tracer
        self._clone_env = dict(clone_env) if clone_env is not None else None

    # ── Cloning + worktrees ───────────────────────────────────────────────

    @GitOperationDeclaration("clone")
    def clone(self, url: str, dest: Path, *, repo_name: str, branch: str | None = None) -> None:
        try:
            if branch is None:
                git.Repo.clone_from(url, str(dest), env=self._clone_env)
            else:
                # `--branch` checks out `branch` tracking `origin/<branch>` rather
                # than the remote's default HEAD; git removes `dest` itself when
                # the remote lacks the branch, so no partial checkout survives.
                git.Repo.clone_from(url, str(dest), env=self._clone_env, branch=branch)
        except git.GitCommandError as exc:
            message = f"clone failed for {url}"
            if branch is not None and _is_missing_remote_branch(exc, branch):
                message = f"clone failed for {url}: branch '{branch}' not found on the remote"
            raise self._error_factory.from_git(exc, message=message, cwd=dest.parent) from exc

    @GitOperationDeclaration("worktree add")
    def add_worktree(
        self,
        source: Path,
        worktree_path: Path,
        branch: str,
        base_branch: str | None = None,
        *,
        repo_name: str,
        env: str | None,
    ) -> None:
        try:
            with git.Repo(str(source)) as r:
                if base_branch is None:
                    r.git.worktree("add", str(worktree_path), branch)
                else:
                    # `--no-track` keeps the new branch from being born with an
                    # incidental upstream (e.g. under `branch.autoSetupMerge =
                    # always`, or when base_branch resolves to a remote-tracking
                    # ref) so init's own tracking logic — `_connect_inferred_upstream`
                    # for non-pinned repos, `_configure_pinned_tracking` for pinned
                    # ones — is the sole authority over the branch's upstream.
                    r.git.worktree("add", str(worktree_path), "-b", branch, "--no-track", base_branch)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git worktree add failed at {worktree_path}",
                cwd=source,
            ) from exc

    @GitOperationDeclaration("worktree remove")
    def remove_worktree(
        self, source: Path, worktree_path: Path, force: bool, *, repo_name: str, env: str | None
    ) -> None:
        try:
            with git.Repo(str(source)) as r:
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

    @GitOperationExemption("env discovery is workspace discovery, not an operation on a repository")
    def list_worktrees(self, source: Path) -> list[Path]:
        try:
            with git.Repo(str(source)) as r:
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

    @GitOperationDeclaration("branch")
    def get_local_branches(self, path: Path, *, repo_name: str, env: str | None) -> list[str]:
        with git.Repo(str(path)) as r:
            return [h.name for h in r.heads]

    @GitOperationDeclaration("config")
    def get_tracking_branch(self, path: Path, *, repo_name: str, env: str | None) -> str | None:
        with git.Repo(str(path)) as r:
            try:
                tb = r.active_branch.tracking_branch()
            except (TypeError, ValueError):
                return None
            return tb.name if tb is not None else None

    @GitOperationDeclaration("config")
    def set_upstream_to(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> None:
        # Write branch.<head>.{remote,merge} config directly instead of
        # `git branch --set-upstream-to <ref>`, which exits 128 when <ref> is a
        # remote-tracking branch git cannot resolve locally — the state left by
        # `winter ws connect <env> feature/foo` on a branch that was never
        # pushed. Setting the config directly tolerates that pre-push state
        # (the same way `IWriteRepoRepository.set_upstream` does for connect)
        # and is equivalent to `--set-upstream-to` when the remote ref exists.
        #
        # Unlike `IWriteRepoRepository.set_upstream`, this method hard-fails on a
        # detached HEAD rather than falling back and warning — deliberately: this
        # runs unattended across every repo during `winter ws init`, where a
        # detached worktree signals something is already wrong and needs surfacing,
        # not a silent write to a branch name HEAD isn't on. See
        # `write_repo_repository.py`'s `set_upstream` for the connect-path rationale.
        remote, _, branch = ref.partition("/")
        if not branch:
            raise RepoError(f"set_upstream_to: expected '<remote>/<branch>', got {ref!r}")
        try:
            with git.Repo(str(path)) as r:
                try:
                    head = r.active_branch.name
                except TypeError as exc:
                    # GitPython raises TypeError specifically for a detached HEAD
                    # (see `internal/branch_tracking.py`'s documented split).
                    raise self._error_factory.from_exception(
                        exc,
                        message=f"set-upstream-to {ref} failed at {path}: HEAD is detached",
                        cwd=path,
                    ) from exc
                except ValueError as exc:
                    # ValueError covers more than an unborn HEAD — e.g. a worktree
                    # orphaned by a deleted/re-cloned source checkout raises
                    # `ValueError: Reference at 'HEAD' does not exist`. Surface the
                    # library's own message rather than asserting one diagnosis.
                    raise self._error_factory.from_exception(
                        exc,
                        message=f"set-upstream-to {ref} failed at {path}: {exc}",
                        cwd=path,
                    ) from exc
                r.git.config(f"branch.{head}.remote", remote)
                r.git.config(f"branch.{head}.merge", f"refs/heads/{branch}")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"set-upstream-to {ref} failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("config")
    def set_push_default_upstream(self, path: Path, *, repo_name: str, env: str | None) -> None:
        with git.Repo(str(path)) as r, r.config_writer() as cw:
            cw.set_value("push", "default", "upstream")

    # ── Repository-scope config ──────────────────────────────────────────

    @GitOperationDeclaration("config")
    def set_user_identity(self, path: Path, name: str, email: str, *, repo_name: str, env: str | None) -> None:
        with git.Repo(str(path)) as r, r.config_writer(config_level="repository") as cw:
            cw.set_value("user", "name", name)
            cw.set_value("user", "email", email)

    @GitOperationDeclaration("config")
    def get_push_default(self, path: Path, *, repo_name: str, env: str | None) -> str | None:
        with git.Repo(str(path)) as r, r.config_reader() as cr:
            value = cr.get_value("push", "default", "")
        return str(value) if value != "" else None

    # ── Status probes ────────────────────────────────────────────────────

    @GitOperationDeclaration("status")
    def is_worktree_clean(self, path: Path, *, repo_name: str, env: str | None) -> bool:
        """True iff `git status --porcelain` reports no changes.

        Any failure (missing repo, git error) returns False so safety-check
        callers (destroy, prune) treat ambiguity as "do not touch".
        """
        try:
            with git.Repo(str(path)) as r:
                output = r.git.status("--porcelain")
        except (git.InvalidGitRepositoryError, git.NoSuchPathError, git.GitCommandError):
            return False
        return not output.strip()

    # ── Ref resolution + checkout ─────────────────────────────────────────

    @GitOperationDeclaration("rev-parse")
    def resolve_ref(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> tuple[RefKind, str]:
        """Classify `ref` against on-disk refs and return its kind + full 40-char SHA.

        Tries candidates in order via ``git rev-parse --verify``; first match wins.
        Raises ``RepoError`` if none resolve.
        """
        candidates: list[tuple[str, RefKind]] = [
            (f"refs/remotes/origin/{ref}", RefKind.branch),
            (f"refs/tags/{ref}", RefKind.tag),
            (f"{ref}^{{commit}}", RefKind.commit),
        ]
        with git.Repo(str(path)) as r:
            for candidate, kind in candidates:
                try:
                    sha = r.git.rev_parse("--verify", candidate).strip()
                    return kind, sha
                except git.GitCommandError:
                    continue
        raise RepoError(
            f"unresolvable ref {ref!r} at {path}: not a branch, tag, or commit SHA",
            cwd=str(path),
        )

    @GitOperationDeclaration("checkout")
    def checkout_detached(self, path: Path, commit: str, *, repo_name: str, env: str | None) -> None:
        """Check out `commit` in detached-HEAD mode."""
        try:
            with git.Repo(str(path)) as r:
                r.git.checkout("--detach", commit)
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"checkout --detach {commit} failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("checkout")
    def checkout_branch(self, path: Path, branch: str, *, repo_name: str, env: str | None) -> None:
        """Land the working tree on the local branch tracking ``origin/<branch>``.

        Creates the local branch with upstream set if it does not yet exist.
        """
        try:
            with git.Repo(str(path)) as r:
                r.git.checkout("-B", branch, "--track", f"origin/{branch}")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"checkout -B {branch} --track origin/{branch} failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("rev-parse")
    def get_head_commit(self, path: Path, *, repo_name: str, env: str | None) -> str:
        """Return the full 40-character SHA of HEAD."""
        try:
            with git.Repo(str(path)) as r:
                return r.git.rev_parse("HEAD").strip()
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"rev-parse HEAD failed at {path}",
                cwd=path,
            ) from exc
        except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
            raise self._error_factory.from_exception(
                exc,
                message=f"rev-parse HEAD failed at {path}: not a git repository",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("write-tree")
    def get_tracked_content_tree(self, path: Path, *, repo_name: str, env: str | None) -> str:
        """Return the tree of the tracked content: the index, brought up to the working tree.

        The throwaway ``GIT_INDEX_FILE`` is a byte copy of the repo's real index
        (``shutil.copy2``, so the file's mtime survives and git's racy-timestamp
        rule keeps rehashing entries written in the same tick as the index), so
        staged adds, renames and removals are part of the starting state and
        ``git add -u`` then folds in every working-tree edit and deletion of a
        tracked file. The real index is never written. A repo with no index
        file seeds the copy from ``HEAD`` instead.

        The only repository writes are the unreferenced blob and tree objects
        that ``add -u`` and ``write-tree`` leave in the object store.
        """
        try:
            with (
                tempfile.TemporaryDirectory(prefix="winter-fingerprint-") as tmp,
                git.Repo(str(path)) as r,
            ):
                temp_index = Path(tmp) / "index"
                real_index = Path(path) / r.git.rev_parse("--git-path", "index").strip()
                with r.git.custom_environment(GIT_INDEX_FILE=str(temp_index)):
                    if real_index.is_file():
                        shutil.copy2(real_index, temp_index)
                    else:
                        r.git.read_tree("HEAD")
                    r.git.add("-u")
                    return r.git.write_tree().strip()
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"tracked-content tree failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("status")
    def has_tracked_changes(self, path: Path, *, repo_name: str, env: str | None) -> bool:
        """True iff a tracked file is staged, modified, deleted or conflicted.

        ``git status --porcelain --untracked-files=no`` is the same judgement
        ``ws status`` makes of tracked files; ``GIT_OPTIONAL_LOCKS=0`` keeps
        status from refreshing (rewriting) the real index.
        """
        try:
            with (
                git.Repo(str(path)) as r,
                r.git.custom_environment(GIT_OPTIONAL_LOCKS="0"),
            ):
                return bool(r.git.status("--porcelain", "--untracked-files=no").strip())
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"status failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("stash push")
    def stash_push(self, path: Path, *, repo_name: str, env: str | None) -> None:
        """Stash the working tree at `path`."""
        try:
            with git.Repo(str(path)) as r:
                r.git.stash("push")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git stash push failed at {path}",
                cwd=path,
            ) from exc

    @GitOperationDeclaration("stash pop")
    def stash_pop(self, path: Path, *, repo_name: str, env: str | None) -> None:
        """Pop the most recent stash at `path`."""
        try:
            with git.Repo(str(path)) as r:
                r.git.stash("pop")
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"git stash pop failed at {path}",
                cwd=path,
            ) from exc


def _conforms_gitpython_repository(x: GitPythonRepository) -> IGitRepository:
    return x


def _is_missing_remote_branch(exc: git.GitCommandError, branch: str) -> bool:
    """True when `git clone --branch <branch>` failed because the remote has no such branch."""
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    return f"Remote branch {branch} not found" in stderr
