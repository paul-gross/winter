from __future__ import annotations

from pathlib import Path
from typing import Protocol

from winter_cli.modules.workspace.models.domain_model import RefKind


class IGitRepository(Protocol):
    """Service-level seam for the imperative git operations that init/destroy/prune perform.

    Distinct from `IReadRepoRepository` / `IWriteRepoRepository` (which model
    *winter's domain*: feature worktrees, syncing, etc.) — this Protocol
    exposes raw git verbs the lifecycle services need. The split keeps each
    surface focused: domain repositories speak in `FeatureWorktree` /
    `StandaloneRepository`, this one speaks in paths.

    Every method raises `RepoError` on git failure — GitPython types are
    confined to the adapter under `internal/`.

    Every method except `list_worktrees` takes a required keyword `repo_name`: the
    name of the repo the path belongs to, from the repo object the caller holds.
    Every path-taking method except `clone` and `list_worktrees` also takes a
    required keyword `env`: the name of the feature environment the call acts in,
    or `None` when the path is not a feature worktree (a source checkout, a
    standalone). Both label the call's trace span and change nothing else; the env
    is never derived from the path. `list_worktrees` is env discovery and opens no
    span.
    """

    # ── Cloning + worktrees ───────────────────────────────────────────────

    def clone(self, url: str, dest: Path, *, repo_name: str, branch: str | None = None) -> None:
        """Clone `url` into `dest`.

        With `branch`, the clone checks out that branch tracking `origin/<branch>`
        whatever the remote's default HEAD is; a `branch` the remote lacks raises
        `RepoError` naming it and leaves nothing at `dest`. Without it, the clone
        checks out the remote's default HEAD.
        """
        ...

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
        """Create a new git worktree at `worktree_path`.

        When `base_branch` is None the branch is assumed to already exist
        locally and is attached; when supplied, a new branch is created from
        `base_branch`. Matches the `git worktree add [-b <branch> <base>]`
        forms.
        """
        ...

    def remove_worktree(
        self, source: Path, worktree_path: Path, force: bool, *, repo_name: str, env: str | None
    ) -> None: ...

    def list_worktrees(self, source: Path) -> list[Path]: ...

    # ── Branches + tracking ──────────────────────────────────────────────

    def get_local_branches(self, path: Path, *, repo_name: str, env: str | None) -> list[str]: ...

    def get_tracking_branch(self, path: Path, *, repo_name: str, env: str | None) -> str | None:
        """Return the current branch's tracking ref (e.g. `origin/main`), or None if unset / detached."""
        ...

    def set_upstream_to(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> None: ...

    def set_push_default_upstream(self, path: Path, *, repo_name: str, env: str | None) -> None:
        """Set `push.default=upstream` so `git push` from the worktree branch targets its tracking branch."""
        ...

    # ── Repository-scope config ──────────────────────────────────────────

    def set_user_identity(self, path: Path, name: str, email: str, *, repo_name: str, env: str | None) -> None: ...

    def get_push_default(self, path: Path, *, repo_name: str, env: str | None) -> str | None: ...

    # ── Status probes ────────────────────────────────────────────────────

    def is_worktree_clean(self, path: Path, *, repo_name: str, env: str | None) -> bool:
        """True if the worktree has no uncommitted/untracked changes.

        Returns False on any git failure — callers use this for safety
        checks (destroy, prune) where "I don't know" must be treated as
        "do not touch".
        """
        ...

    # ── Ref resolution + checkout ─────────────────────────────────────────

    def resolve_ref(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> tuple[RefKind, str]:
        """Classify `ref` against on-disk refs and return its kind + full 40-char SHA.

        Resolution order (first match wins):
          1. ``refs/remotes/origin/<ref>`` → ``RefKind.branch``
          2. ``refs/tags/<ref>``           → ``RefKind.tag``
          3. ``<ref>^{commit}``            → ``RefKind.commit`` (raw SHA or abbrev)

        Uses ``git rev-parse --verify`` per candidate — no network access.
        Raises ``RepoError`` if none of the candidates resolve, naming `path` and `ref`.
        """
        ...

    def checkout_detached(self, path: Path, commit: str, *, repo_name: str, env: str | None) -> None:
        """Check out `commit` in detached-HEAD mode (equivalent to ``git checkout --detach <commit>``)."""
        ...

    def checkout_branch(self, path: Path, branch: str, *, repo_name: str, env: str | None) -> None:
        """Land the working tree on the local branch tracking ``origin/<branch>``.

        Creates the local branch and sets its upstream if it does not yet exist.
        When the branch already exists, ``-B`` FORCE-RESETS the local branch pointer
        to ``origin/<branch>`` — this is NOT a fast-forward and CAN silently discard
        local commits. Callers that want ff-only safety must check for divergence
        themselves before calling this, or use ``integrate_standalone_to_ref``
        instead (which wraps ``git merge --ff-only``).

        Intended for: init's fresh-clone checkout of a branch pin (always clean),
        and ``update_pins`` explicit re-pin where the caller has already applied
        the dirty-tree guard and the intent is to force-land on the remote tip.
        NOT for branch-pin advances on pull, which use ``integrate_standalone_to_ref``.
        """
        ...

    def get_head_commit(self, path: Path, *, repo_name: str, env: str | None) -> str:
        """Return the full 40-character SHA of HEAD."""
        ...

    def get_tracked_content_tree(self, path: Path, *, repo_name: str, env: str | None) -> str:
        """Return the SHA of the tree of the repo's tracked content: the index, brought up to the working tree.

        Built in a throwaway index (a byte copy of the real ``GIT_INDEX_FILE``,
        then ``git add -u``, then ``git write-tree``), so staged adds, renames
        and removals count, and every edit or deletion of a tracked file shows
        up; untracked files never do. The real index, refs and working tree are
        never written; the only side effect is unreferenced objects in the
        object store. For a clean repo this equals ``HEAD^{tree}``.
        """
        ...

    def has_tracked_changes(self, path: Path, *, repo_name: str, env: str | None) -> bool:
        """True iff a tracked file is staged, modified, deleted or conflicted (untracked files are ignored).

        The judgement ``ws status`` makes of tracked files; never refreshes the real index.
        """
        ...

    def stash_push(self, path: Path, *, repo_name: str, env: str | None) -> None:
        """Stash the working tree at `path` (equivalent to ``git stash push``)."""
        ...

    def stash_pop(self, path: Path, *, repo_name: str, env: str | None) -> None:
        """Pop the most recent stash at `path` (equivalent to ``git stash pop``).

        Best-effort: called after checkout to restore a dirty tree stashed by
        `stash_push`. If the pop fails (e.g. conflict or empty stash), it
        raises `RepoError`.
        """
        ...
