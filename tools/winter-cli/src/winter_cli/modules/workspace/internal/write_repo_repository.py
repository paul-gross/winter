from __future__ import annotations

import logging

import git

from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.read_repo_repository import ReadRepoRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureWorktree,
    MergeMode,
    MergeResult,
    ProjectRepository,
    PullMode,
    RepoError,
    RepoMergeOutcome,
    RepoSyncOutcome,
    StandaloneRepository,
    SyncResult,
)
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository

logger = logging.getLogger(__name__)


def _autostash_args(autostash: bool) -> list[str]:
    return ["--autostash"] if autostash else []


class WriteRepoRepository(ReadRepoRepository):
    """Read-write GitPython implementation. Extends ReadRepoRepository with mutating operations."""

    def __init__(self, error_factory: RepoErrorFactory, git_ops: GitOpsService) -> None:
        super().__init__(error_factory)
        self._git_ops = git_ops

    def fetch(self, worktree: FeatureWorktree) -> None:
        # Shell out via r.git rather than r.remotes.origin.fetch() — gitpython's
        # high-level remotes API reads from the worktree's git-dir, which doesn't
        # have remote config; the shared remotes live in the common-dir.
        with git.Repo(str(worktree.path)) as r:
            self._git_ops.run_remote_git(
                r,
                "fetch",
                "origin",
                cwd=worktree.path,
                message=f"fetch failed for {worktree.repository.name}",
            )

    def integrate(
        self,
        worktree: FeatureWorktree,
        target_ref: str,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        with git.Repo(str(worktree.path)) as r:
            return self._integrate(
                r,
                worktree.repository.name,
                target_ref,
                mode,
                autostash,
            )

    def merge_ref(
        self,
        worktree: FeatureWorktree,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        """Merge `source_ref` into the worktree's branch — pull-style semantics.

        Mirrors `integrate`'s mode handling so merge's failure modes match
        pull's: conflicts (or autostash failures) abort and report diverged
        rather than leaving an in-progress merge. The only signal merge
        adds is `skipped_missing_ref` — pull's source ref is always the
        tracked upstream, so it can't be missing; merge takes an arbitrary
        ref, so a typo or per-repo absence is a real case.
        """
        with git.Repo(str(worktree.path)) as r:
            return self._merge(
                r,
                worktree.repository.name,
                source_ref,
                mode,
                autostash,
            )

    def merge_ref_standalone(
        self,
        repo: StandaloneRepository,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        """Standalone counterpart to `merge_ref` — same modes and outcome shape."""
        with git.Repo(str(repo.path)) as r:
            return self._merge(
                r,
                repo.name,
                source_ref,
                mode,
                autostash,
            )

    def sync_ff_only(self, repo: ProjectRepository) -> int:
        """Fetch origin and fast-forward the source checkout's local main.

        Returns the number of commits the local main advanced (0 when it was
        already up to date), so `ws fetch` can report `+N` per repo.
        """
        main_branch = repo.main_branch
        with git.Repo(str(repo.main_path)) as r:
            self._git_ops.run_remote_git(
                r,
                "fetch",
                "origin",
                cwd=repo.main_path,
                message=f"sync_ff_only failed for {repo.name}",
            )
            head_before = r.head.commit.hexsha
            try:
                r.git.merge("--ff-only", f"origin/{main_branch}")
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"sync_ff_only failed for {repo.name}",
                    cwd=repo.main_path,
                ) from exc
            head_after = r.head.commit.hexsha
            if head_before == head_after:
                return 0
            return self._count_range(r, repo.name, f"{head_before}..{head_after}")

    def set_upstream(self, worktree: FeatureWorktree, remote_branch: str) -> None:
        # Write branch.<head>.{remote,merge} directly instead of using
        # `git branch --set-upstream-to`, which refuses to set tracking to a
        # remote ref it can't see locally. Setting it directly lets connect
        # succeed on a brand-new feature branch with no remote ref yet — the
        # first push then creates it on origin.
        with git.Repo(str(worktree.path)) as r:
            remote, _, branch = remote_branch.partition("/")
            if not branch:
                raise RepoError(f"set_upstream: expected '<remote>/<branch>', got {remote_branch!r}")
            head = r.active_branch.name
            r.git.config(f"branch.{head}.remote", remote)
            r.git.config(f"branch.{head}.merge", f"refs/heads/{branch}")

    def has_local_ref(self, worktree: FeatureWorktree, ref: str) -> bool:
        """Whether `ref` resolves in the worktree's local object store. No network.

        Catches `GitCommandError` deliberately: `rev-parse --verify --quiet`
        exits non-zero when the ref doesn't resolve, which is the *answer*
        to this method's question, not an error.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                r.git.rev_parse("--verify", "--quiet", ref)
                return True
            except git.GitCommandError:
                return False

    def is_worktree_dirty(self, worktree: FeatureWorktree) -> bool:
        """Staged or unstaged changes present? Untracked files don't count —
        `git reset --hard` leaves untracked files in place."""
        with git.Repo(str(worktree.path)) as r:
            return r.is_dirty(working_tree=True, index=True, untracked_files=False)

    def count_commits_not_in(self, worktree: FeatureWorktree, ref: str) -> int:
        """Commits reachable from HEAD but not from `ref`. No network."""
        with git.Repo(str(worktree.path)) as r:
            try:
                return int(r.git.rev_list("--count", "HEAD", f"^{ref}"))
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"count_commits_not_in failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def hard_reset(self, worktree: FeatureWorktree, target_ref: str) -> None:
        with git.Repo(str(worktree.path)) as r:
            try:
                r.git.reset("--hard", target_ref)
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"reset failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def unset_upstream(self, worktree: FeatureWorktree) -> None:
        """Remove upstream tracking; no-op when already unset.

        Probes `branch.<head>.remote` first: `git config --get` exits 1
        specifically for "key not found," which lets us distinguish the
        idempotent-disconnect case from real config-write failures. If the
        upstream isn't configured we return without touching anything; if
        the actual `--unset-upstream` call fails, that raises.
        """
        with git.Repo(str(worktree.path)) as r:
            head = r.active_branch.name
            try:
                r.git.config("--get", f"branch.{head}.remote")
            except git.GitCommandError as exc:
                if exc.status == 1:
                    return  # already unset
                raise self._error_factory.from_git(
                    exc,
                    message=f"probing upstream config failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc
            try:
                r.git.branch("--unset-upstream")
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"unset_upstream failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def get_worktree_upstream(self, worktree: FeatureWorktree) -> str | None:
        """The worktree branch's current upstream (e.g. `origin/feature-123`), or None. No network."""
        with git.Repo(str(worktree.path)) as r:
            return self._tracking_branch_name(r)

    def set_push_default(self, worktree: FeatureWorktree) -> None:
        with git.Repo(str(worktree.path)) as r, r.config_writer() as cw:
            cw.set_value("push", "default", "upstream")

    def push(self, worktree: FeatureWorktree, feature_branch: str | None = None) -> int:
        # `get_worktree_status` opens its own Repo internally; resolve the
        # ahead count before opening ours so the two Repo lifetimes don't
        # overlap unnecessarily.
        status = self.get_worktree_status(worktree)
        # Count what we're actually putting on the remote: when the upstream
        # ref exists, that's `tracking_ahead` (HEAD past upstream). On the
        # first push to a freshly-connected feature branch the upstream ref
        # doesn't exist yet and `tracking_ahead` is 0 — fall back to `ahead`
        # (HEAD past master), which is what the push creates on the remote.
        commit_count = status.tracking_ahead if status.tracking_ref_present else status.ahead
        message = f"push failed for {worktree.repository.name}"
        with git.Repo(str(worktree.path)) as r:
            if feature_branch:
                self._git_ops.run_remote_git(
                    r,
                    "push",
                    "-u",
                    "origin",
                    f"HEAD:refs/heads/{feature_branch}",
                    cwd=worktree.path,
                    message=message,
                )
            else:
                self._git_ops.run_remote_git(
                    r,
                    "push",
                    "origin",
                    cwd=worktree.path,
                    message=message,
                )
        return commit_count

    def fetch_standalone(self, repo: StandaloneRepository) -> None:
        with git.Repo(str(repo.path)) as r:
            self._git_ops.run_remote_git(
                r,
                "fetch",
                "origin",
                cwd=repo.path,
                message=f"fetch failed for {repo.name}",
            )

    def integrate_standalone(
        self,
        repo: StandaloneRepository,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        with git.Repo(str(repo.path)) as r:
            tb = self._tracking_branch_name(r)
            if tb is None:
                return RepoSyncOutcome(repo_name=repo.name, sync_result=SyncResult.no_upstream)
            return self._integrate(r, repo.name, tb, mode, autostash)

    def push_standalone(self, repo: StandaloneRepository) -> int:
        with git.Repo(str(repo.path)) as r:
            if self._tracking_branch_name(r) is None:
                raise RepoError(
                    f"{repo.name} has no upstream — set one with `git branch --set-upstream-to`",
                    cwd=str(repo.path),
                )
            commit_count = self._tracking_ahead(repo, r)
            self._git_ops.run_remote_git(
                r,
                "push",
                "origin",
                cwd=repo.path,
                message=f"push failed for {repo.name}",
            )
            return commit_count

    def get_standalone_tracking_ahead(self, repo: StandaloneRepository) -> int:
        with git.Repo(str(repo.path)) as r:
            return self._tracking_ahead(repo, r)

    def get_standalone_upstream(self, repo: StandaloneRepository) -> str | None:
        with git.Repo(str(repo.path)) as r:
            return self._tracking_branch_name(r)

    def _integrate(
        self,
        r: git.Repo,
        repo_name: str,
        target_ref: str,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        # Commits the upstream is ahead of HEAD *before* we integrate — the
        # number brought in by a clean ff / merge / rebase. Resolved once
        # up-front (HEAD moves during a ff) and reported as `commits` on every
        # success outcome; 0 means already up to date. Diverged outcomes carry
        # their own ahead/behind span instead.
        commits = self._count_range(r, repo_name, f"HEAD..{target_ref}")
        if mode == PullMode.ff_only:
            return self._ff_only(r, repo_name, target_ref, autostash, commits)
        if mode == PullMode.merge:
            return self._ff_or_merge(r, repo_name, target_ref, autostash, commits)
        if mode == PullMode.rebase:
            return self._ff_or_rebase(r, repo_name, target_ref, autostash, commits)
        raise ValueError(f"unknown PullMode: {mode}")

    def _ff_only(self, r: git.Repo, repo_name: str, target_ref: str, autostash: bool, commits: int) -> RepoSyncOutcome:
        head_before = r.head.commit.hexsha
        try:
            r.git.merge(*_autostash_args(autostash), "--ff-only", target_ref)
        except git.GitCommandError:
            return self._diverged_outcome(r, repo_name, target_ref)
        head_after = r.head.commit.hexsha
        if head_before == head_after:
            return RepoSyncOutcome(repo_name=repo_name, sync_result=SyncResult.up_to_date)
        return RepoSyncOutcome(repo_name=repo_name, sync_result=SyncResult.fast_forwarded, commits=commits)

    def _ff_or_merge(
        self, r: git.Repo, repo_name: str, target_ref: str, autostash: bool, commits: int
    ) -> RepoSyncOutcome:
        ff = self._ff_only(r, repo_name, target_ref, autostash, commits)
        if ff.sync_result != SyncResult.diverged:
            return ff
        try:
            r.git.merge(*_autostash_args(autostash), target_ref)
            return RepoSyncOutcome(repo_name=repo_name, sync_result=SyncResult.merged, commits=commits)
        except git.GitCommandError:
            self._abort(r.git.merge)
            return self._diverged_outcome(r, repo_name, target_ref)

    def _ff_or_rebase(
        self, r: git.Repo, repo_name: str, target_ref: str, autostash: bool, commits: int
    ) -> RepoSyncOutcome:
        ff = self._ff_only(r, repo_name, target_ref, autostash, commits)
        if ff.sync_result != SyncResult.diverged:
            return ff
        try:
            r.git.rebase(*_autostash_args(autostash), target_ref)
            return RepoSyncOutcome(repo_name=repo_name, sync_result=SyncResult.rebased, commits=commits)
        except git.GitCommandError:
            self._abort(r.git.rebase)
            return self._diverged_outcome(r, repo_name, target_ref)

    def _merge(
        self,
        r: git.Repo,
        repo_name: str,
        source_ref: str,
        mode: MergeMode,
        autostash: bool,
    ) -> RepoMergeOutcome:
        if not self._has_ref(r, source_ref):
            return RepoMergeOutcome(
                repo_name=repo_name,
                result=MergeResult.skipped_missing_ref,
                error=f"source ref not found: {source_ref}",
            )
        head_before = r.head.commit.hexsha
        if mode == MergeMode.ff_only:
            return self._merge_ff_only(r, repo_name, source_ref, autostash, head_before)
        if mode == MergeMode.no_ff:
            return self._merge_no_ff(r, repo_name, source_ref, autostash)
        if mode == MergeMode.merge:
            return self._merge_ff_or_commit(r, repo_name, source_ref, autostash, head_before)
        raise ValueError(f"unknown MergeMode: {mode}")

    def _merge_ff_only(
        self,
        r: git.Repo,
        repo_name: str,
        source_ref: str,
        autostash: bool,
        head_before: str,
    ) -> RepoMergeOutcome:
        try:
            r.git.merge(*_autostash_args(autostash), "--ff-only", source_ref)
        except git.GitCommandError:
            return self._diverged_merge_outcome(r, repo_name, source_ref)
        head_after = r.head.commit.hexsha
        if head_before == head_after:
            return RepoMergeOutcome(repo_name=repo_name, result=MergeResult.up_to_date)
        return RepoMergeOutcome(repo_name=repo_name, result=MergeResult.fast_forwarded)

    def _merge_ff_or_commit(
        self,
        r: git.Repo,
        repo_name: str,
        source_ref: str,
        autostash: bool,
        head_before: str,
    ) -> RepoMergeOutcome:
        """`--merge` mode: ff when possible, 3-way merge commit when ff fails.

        Mirrors `_ff_or_merge` (pull's `--merge`): conflicts / autostash
        failures abort and report diverged, no in-progress merge left over.
        """
        ff = self._merge_ff_only(r, repo_name, source_ref, autostash, head_before)
        if ff.result != MergeResult.diverged:
            return ff
        try:
            r.git.merge(*_autostash_args(autostash), source_ref)
            return RepoMergeOutcome(repo_name=repo_name, result=MergeResult.merged)
        except git.GitCommandError:
            self._abort(r.git.merge)
            return self._diverged_merge_outcome(r, repo_name, source_ref)

    def _merge_no_ff(
        self,
        r: git.Repo,
        repo_name: str,
        source_ref: str,
        autostash: bool,
    ) -> RepoMergeOutcome:
        # Short-circuit when source is fully reachable from HEAD — git treats
        # this as "already up to date" and exits 0 without creating a merge
        # commit, which would otherwise mislabel as MergeResult.merged. The
        # check is `behind == 0` (no commits to bring in), not also
        # `ahead == 0`: HEAD may have its own commits past source and still
        # have source fully merged in.
        try:
            behind = int(r.git.rev_list("--count", f"HEAD..{source_ref}"))
        except git.GitCommandError:
            behind = 0
        if behind == 0:
            return RepoMergeOutcome(repo_name=repo_name, result=MergeResult.up_to_date)
        try:
            r.git.merge(*_autostash_args(autostash), "--no-ff", source_ref)
            return RepoMergeOutcome(repo_name=repo_name, result=MergeResult.merged)
        except git.GitCommandError:
            self._abort(r.git.merge)
            return self._diverged_merge_outcome(r, repo_name, source_ref)

    def _diverged_merge_outcome(self, r: git.Repo, repo_name: str, source_ref: str) -> RepoMergeOutcome:
        ahead = 0
        behind = 0
        try:
            ahead = int(r.git.rev_list("--count", f"{source_ref}..HEAD"))
            behind = int(r.git.rev_list("--count", f"HEAD..{source_ref}"))
        except git.GitCommandError as exc:
            logger.warning(
                "diverged ahead/behind probe failed for %s vs %s: %s",
                repo_name,
                source_ref,
                exc.stderr.strip() if isinstance(exc.stderr, str) else exc,
            )
        return RepoMergeOutcome(
            repo_name=repo_name,
            result=MergeResult.diverged,
            ahead=ahead,
            behind=behind,
        )

    @staticmethod
    def _count_range(r: git.Repo, repo_name: str, rev_range: str) -> int:
        """`git rev-list --count <range>`, best-effort.

        Used to report how many commits an operation moved (ff span, commits
        integrated). A count is reporting metadata, not load-bearing — if the
        rev-list fails (e.g. a ref stopped resolving mid-operation) we warn and
        return 0 rather than failing the whole fetch / pull over a number.
        """
        try:
            return int(r.git.rev_list("--count", rev_range))
        except git.GitCommandError as exc:
            logger.warning(
                "commit-count probe failed for %s over %s: %s",
                repo_name,
                rev_range,
                exc.stderr.strip() if isinstance(exc.stderr, str) else exc,
            )
            return 0

    @staticmethod
    def _has_ref(r: git.Repo, ref: str) -> bool:
        try:
            r.git.rev_parse("--verify", "--quiet", f"{ref}^{{commit}}")
            return True
        except git.GitCommandError:
            return False

    def _diverged_outcome(self, r: git.Repo, repo_name: str, target_ref: str) -> RepoSyncOutcome:
        ahead = 0
        behind = 0
        try:
            ahead = int(r.git.rev_list("--count", f"{target_ref}..HEAD"))
            behind = int(r.git.rev_list("--count", f"HEAD..{target_ref}"))
        except git.GitCommandError as exc:
            # Best-effort ahead/behind for a diverged outcome — if rev_list
            # itself fails (typically because target_ref doesn't resolve), we
            # still want to return the diverged result so the caller can react;
            # downgrade to a warning instead of raising.
            logger.warning(
                "diverged ahead/behind probe failed for %s vs %s: %s",
                repo_name,
                target_ref,
                exc.stderr.strip() if isinstance(exc.stderr, str) else exc,
            )
        return RepoSyncOutcome(
            repo_name=repo_name,
            sync_result=SyncResult.diverged,
            ahead=ahead,
            behind=behind,
        )

    @staticmethod
    def _abort(op) -> None:
        # Intentional best-effort cleanup. `--abort` is invoked only after a
        # prior merge/rebase already failed; if abort itself errors there's
        # nothing useful to do — the caller already has a diverged outcome.
        try:
            op("--abort")
        except git.GitCommandError as exc:
            logger.warning(
                "abort cleanup failed: %s",
                exc.stderr.strip() if isinstance(exc.stderr, str) else exc,
            )

    @staticmethod
    def _tracking_branch_name(r: git.Repo) -> str | None:
        try:
            tb = r.active_branch.tracking_branch()
        except TypeError:
            return None
        return tb.name if tb is not None else None

    def _tracking_ahead(self, repo: StandaloneRepository, r: git.Repo) -> int:
        tb = self._tracking_branch_name(r)
        if tb is None:
            return 0
        try:
            return int(r.git.rev_list("--count", f"{tb}..HEAD"))
        except git.GitCommandError as exc:
            raise self._error_factory.from_git(
                exc,
                message=f"tracking-ahead probe failed for {repo.name}",
                cwd=repo.path,
            ) from exc


def _conforms_write_repo_repository(x: WriteRepoRepository) -> IWriteRepoRepository:
    return x
