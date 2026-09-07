from __future__ import annotations

import logging
from pathlib import Path

import git

from winter_cli.modules.workspace.internal.branch_tracking import read_origin_merge_branch
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.read_repo_repository import ReadRepoRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory, unwrap_gitpython_stream
from winter_cli.modules.workspace.models import (
    FeatureWorktree,
    LocalFastForward,
    MergeMode,
    MergeResult,
    PartialCleanError,
    ProjectRepository,
    PullMode,
    RebaseConflict,
    RebaseOntoResult,
    RepoError,
    RepoMergeOutcome,
    RepoSyncOutcome,
    ResetMode,
    StandaloneRepository,
    SyncResult,
)
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository

logger = logging.getLogger(__name__)

# `git clean` has no plumbing/porcelain output mode, so its per-path lines are
# parsed. Both prefixes are gettext-translated, which is why `_run_clean`
# pins `LC_ALL=C` before reading them.
_CLEAN_DRY_RUN_PREFIX = "Would remove "
_CLEAN_REMOVED_PREFIX = "Removing "

# Lines `git clean` emits that name something it is *not* removing. Recognized
# so the fail-closed check below cannot mistake "git only had skips to report"
# for "the prefixes stopped matching". `git clean -nd` reports an untracked
# nested repository this way on the git versions that mention it at all.
_CLEAN_NON_REMOVAL_PREFIXES = ("Would skip ", "Skipping ", "warning:")


def _parse_clean_output(output: str, prefix: str, repo_name: str, *, strict: bool = True) -> list[str]:
    """Paths out of `git clean` output, or raise if the line shape is unrecognized.

    Fails closed rather than returning what it could parse: a silent partial
    parse would under-report the removal set on the one command whose preview
    is the only thing standing in front of an unrecoverable delete. An empty
    `output`, or one carrying only recognized non-removal lines, is the
    legitimate nothing-to-clean case and yields `[]`; output that exists but
    matches nothing known is a broken contract (git reworded the message, or
    the locale pin failed) and raises.

    `strict=False` is for the already-failing path, where this is salvaging a
    partial record from a command that has *already deleted files*: raising a
    second error there would discard the very paths being rescued.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    paths = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
    if paths or not strict:
        return paths
    unexplained = [line for line in lines if not line.startswith(_CLEAN_NON_REMOVAL_PREFIXES)]
    if unexplained:
        raise RepoError(
            message=f"could not parse `git clean` output for {repo_name}",
            subcommand="clean",
            stderr=output,
        )
    return paths


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
            head = self._head_branch_name(r, worktree)
            try:
                r.active_branch  # noqa: B018 — probed only to detect detached HEAD
                is_detached = False
            except TypeError:
                is_detached = True
            if is_detached:
                # winter#159 B5: on a detached worktree there is no real
                # `head` to attach tracking to — `_head_branch_name` falls
                # back to `worktree.environment.name` so this write still
                # succeeds (needed by `EnvCheckoutService.checkout_env`,
                # which force-attaches HEAD onto that exact branch moments
                # before calling here). A bare `winter ws connect` against a
                # still-detached worktree hits this same fallback with no
                # such attach step, so the config write lands on a branch
                # name HEAD isn't actually on — warn rather than fail silently.
                # Deliberately more tolerant than `GitPythonRepository.set_upstream_to`,
                # which hard-fails the same detached state: `connect` is a direct,
                # explicit action against one named worktree, so warn-and-write
                # (rather than refuse) is the right posture here. `set_upstream_to`
                # runs unattended across every repo during `winter ws init`, where a
                # detached worktree is a signal something upstream is already wrong
                # (e.g. a `connect` that hit this same fallback) and silently writing
                # tracking config to a branch name HEAD isn't on would compound it
                # across the whole run instead of surfacing it.
                logger.warning(
                    "set_upstream: %s is detached — writing branch.%s.* tracking config without "
                    "attaching HEAD to it; the worktree stays detached and untracked until something "
                    "else (e.g. `winter ws checkout`) attaches it",
                    worktree.repository.name,
                    head,
                )
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

    def count_commits_not_in(self, worktree: FeatureWorktree, ref: str, from_ref: str = "HEAD") -> int:
        """Commits reachable from `from_ref` (HEAD by default) but not from `ref`. No network.

        `from_ref` lets a caller measure abandonment against a ref other than
        HEAD — needed by `EnvCheckoutService`'s branch-abandonment guard
        (winter#159 B1), which must also measure `refs/heads/<env>` when that
        branch isn't currently checked out, since `force_checkout_env_branch`
        force-moves it regardless of what HEAD points at.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                return int(r.git.rev_list("--count", from_ref, f"^{ref}"))
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"count_commits_not_in failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def get_active_branch_name(self, worktree: FeatureWorktree) -> str | None:
        """The currently checked-out branch name, or None when HEAD is detached. No network."""
        with git.Repo(str(worktree.path)) as r:
            try:
                return r.active_branch.name
            except TypeError:
                return None

    def get_branch_upstream(self, worktree: FeatureWorktree, branch_name: str) -> str | None:
        """`branch_name`'s configured upstream (e.g. `origin/feature-123`), or None. No network.

        Same resolution shape as `get_worktree_upstream`, but keyed off an
        explicit branch rather than the currently checked-out one — needed to
        read `refs/heads/<env>`'s own tracking when that branch isn't HEAD.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                head = r.heads[branch_name]
            except IndexError:
                return None
            tb = head.tracking_branch()
            if tb is None:
                return None
            return tb.name if self._has_ref(r, tb.name) else None

    def force_checkout_env_branch(self, worktree: FeatureWorktree, target_ref: str) -> None:
        # Force the worktree onto `worktree.environment.name` — the branch
        # every non-pinned feature worktree is created under
        # (`InitService._create_git_worktree`) — reset to `target_ref`,
        # instead of plain `git reset --hard <target_ref>`. `reset --hard`
        # moves whatever HEAD currently points at but does not *attach* a
        # detached HEAD, so a worktree left detached (e.g. by a manual `git
        # checkout --detach`) stayed detached afterward and the very next
        # Phase 2 step — `set_upstream`'s branch-name read — crashed
        # (winter#159). `checkout -B` re-creates/attaches that branch pointed
        # at `target_ref`; `--force` discards any uncommitted changes exactly
        # as `reset --hard` did (untracked files are left alone either way).
        # `--no-track` stops the checkout from writing its own upstream
        # config from `target_ref` — `set_upstream` remains the sole writer
        # of tracking, preserving Phase 2's reset-before-connect ordering
        # guarantee: a mid-loop failure between this and `set_upstream` still
        # leaves the worktree on its original tracking, not silently
        # re-pointed.
        branch = worktree.environment.name
        with git.Repo(str(worktree.path)) as r:
            try:
                r.git.checkout("--force", "-B", branch, "--no-track", target_ref)
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"reset failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def reset_to(self, worktree: FeatureWorktree, mode: ResetMode, target_ref: str) -> None:
        """`git reset --soft|--mixed|--hard <target_ref>` — the literal, unmodified
        git primitive `winter ws reset` needs.

        Deliberately distinct from `force_checkout_env_branch`: that method
        backs `ws checkout`'s connect-and-reset step via `git checkout
        --force -B <env-branch> --no-track <target_ref>`, which *attaches* a
        possibly detached HEAD onto the env branch as a side effect
        (winter#159's fix) — exactly the opposite of what `ws reset` wants,
        since `reset` is deliberately the half of the checkout/reset split
        that never touches which branch is checked out or its tracking
        config. `reset_to` only ever moves the pointer of whatever is
        currently checked out (staying detached if it was detached, matching
        plain `git reset`), and never writes `branch.<name>.{remote,merge}` —
        `set_upstream` remains the sole writer of tracking, same invariant
        `force_checkout_env_branch` preserves.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                r.git.reset(f"--{mode.value}", target_ref)
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"reset failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def rebase_onto(self, worktree: FeatureWorktree, newbase: str, oldbase: str, branch: str) -> RebaseOntoResult:
        """`git rebase --onto <newbase> <oldbase> <branch>` — replay `branch`'s
        own commits past `oldbase` onto `newbase`, checking `branch` out as a
        side effect (git's own behavior when a branch argument is given).

        Deliberately never aborts on conflict — the opposite postcondition
        from `_ff_or_rebase` below, which `ws pull --rebase` depends on. A
        conflict here leaves the worktree mid-rebase and returns the detail,
        regardless of which rebase backend (`rebase.backend` — merge or
        apply) actually ran; an unexpected failure that isn't a
        conflict-stop under either backend's on-disk state still raises,
        same as every other write op.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                r.git.rebase("--onto", newbase, oldbase, branch)
            except git.GitCommandError as exc:
                conflict = self._read_rebase_conflict(r)
                if conflict is None:
                    raise self._error_factory.from_git(
                        exc,
                        message=f"rebase_onto failed for {worktree.repository.name}",
                        cwd=worktree.path,
                    ) from exc
                return RebaseOntoResult(conflict=conflict)
            return RebaseOntoResult()

    def _read_rebase_conflict(self, r: git.Repo) -> RebaseConflict | None:
        """The detail of a rebase git just stopped mid-way through, or None
        when the worktree isn't actually left mid-rebase at all — i.e. the
        failure `rebase_onto` caught wasn't a conflict-stop, and the caller
        should raise instead of reporting a conflict that never happened.

        `git rebase --onto` runs under whichever backend `rebase.backend`
        selects — the merge backend (default since git 2.26) leaves its
        state under `rebase-merge/`, the older apply backend under
        `rebase-apply/` — the same two directories `is_rebase_in_progress`
        already probes to answer "mid rebase?" honestly; the two checks
        have to agree on what that means, so this probes both in the same
        way rather than assuming the merge backend alone. Each backend
        records the commit it was replaying when it stopped under its own
        directory, by a different name — `stopped-sha` for the merge
        backend, `original-commit` for the apply backend — so both are
        checked, in order, and whichever one is actually present on disk
        supplies `replayed_commit`. Conflicted paths come from the unmerged
        entries `git diff` itself reports, not from parsing `git status`,
        which is already backend-agnostic.
        """
        for relative in ("rebase-merge/stopped-sha", "rebase-apply/original-commit"):
            replayed_commit_path = self._git_path(r, relative)
            if replayed_commit_path is not None and replayed_commit_path.exists():
                replayed_commit = replayed_commit_path.read_text().strip()
                conflicted_paths = [p for p in r.git.diff("--name-only", "--diff-filter=U").splitlines() if p]
                return RebaseConflict(replayed_commit=replayed_commit, conflicted_paths=conflicted_paths)
        return None

    def list_untracked(self, worktree: FeatureWorktree) -> list[str]:
        """Worktree-relative paths `clean_untracked` would remove. No network.

        Backed by `git clean -nd` — the dry run of the very command
        `clean_untracked` executes — rather than by `git ls-files --others
        --exclude-standard`. The two do not agree, in both directions: an
        empty untracked directory is invisible to `ls-files` but *is* removed
        by `clean -fd`, and an untracked nested git repository is listed by
        `ls-files` but *skipped* by `clean -fd`. Deriving the preview from a
        second command would therefore under-report a path this command
        silently deletes and over-report one it leaves behind — unacceptable
        for the only preview standing in front of an unrecoverable delete.

        Reports whatever granularity git itself reports: a whole untracked
        directory appears as one trailing-slash entry (`scratch/`), not as its
        individual files, because that is the unit `clean -fd` removes.
        """
        return self._run_clean(worktree, "-nd", _CLEAN_DRY_RUN_PREFIX)

    def clean_untracked(self, worktree: FeatureWorktree) -> list[str]:
        """`git clean -fd` — remove untracked files and untracked directories,
        returning the paths git reports as actually removed.

        Returns the executed set rather than trusting a prior enumeration, so
        the report cannot claim a deletion that did not happen (a nested git
        repository is enumerable but not removable).

        Deliberately never passes `-x` or `-X`: ignored files stay. In a winter
        worktree those are the provisioned artifacts (`.venv`, `node_modules`,
        build output), so removing them would silently turn a clean into a
        re-provision. A caller who genuinely wants them gone runs `git clean
        -fdx` in the one worktree they mean, where the blast radius is visible;
        there is deliberately no flag that fans that across matched worktrees.

        `-f` is required by git itself (`clean.requireForce` defaults true) and
        carries no winter-level meaning — it is not the `--force` that skips
        the confirmation prompt, which is handled entirely in the handler.
        """
        return self._run_clean(worktree, "-fd", _CLEAN_REMOVED_PREFIX)

    def _run_clean(self, worktree: FeatureWorktree, flags: str, prefix: str) -> list[str]:
        """`git clean <flags>`, parsed into the paths git named.

        Forces `LC_ALL=C` because the line prefixes below are translated
        strings: under a non-English locale an unforced parse would silently
        yield an empty list, which reads as "nothing to clean" for `-nd` and as
        "removed nothing" for `-fd`.

        The `git.Repo(...)` construction is inside the wrapped region because
        it — not the `clean` call — is what raises for a path that is missing
        or not a repository. Left outside, a worktree configured but absent on
        disk escaped as a raw `NoSuchPathError` traceback mid-loop, after
        earlier worktrees had already been cleaned.
        """
        try:
            with git.Repo(str(worktree.path)) as r, r.git.custom_environment(LC_ALL="C", LANGUAGE="C", LC_MESSAGES="C"):
                output = r.git.clean(flags)
        except (git.NoSuchPathError, git.InvalidGitRepositoryError) as exc:
            raise self._error_factory.from_exception(
                exc,
                message=f"clean failed for {worktree.repository.name}: {worktree.path} is not a git worktree",
                cwd=worktree.path,
            ) from exc
        except git.GitCommandError as exc:
            # `git clean -fd` is not transactional: it deletes what it can,
            # warns on the rest, and exits non-zero — so stdout may name paths
            # that are already gone. Carry them; they are unrecoverable and
            # this is the only record of them.
            raw_stdout = unwrap_gitpython_stream(getattr(exc, "stdout", "") or "")
            removed = _parse_clean_output(raw_stdout, prefix, worktree.repository.name, strict=False)
            base = self._error_factory.from_git(
                exc,
                message=f"clean failed for {worktree.repository.name}",
                cwd=worktree.path,
            )
            raise PartialCleanError(
                base.message,
                removed=removed,
                subcommand=base.subcommand,
                cmd_args=base.cmd_args,
                cwd=base.cwd,
                exit_code=base.exit_code,
                stderr=base.stderr,
            ) from exc
        return _parse_clean_output(output, prefix, worktree.repository.name)

    def unset_upstream(self, worktree: FeatureWorktree) -> None:
        """Remove upstream tracking; no-op when already unset.

        Probes `branch.<head>.remote` first: `git config --get` exits 1
        specifically for "key not found," which lets us distinguish the
        idempotent-disconnect case from real config-write failures. If the
        upstream isn't configured we return without touching anything; if
        the actual `--unset-upstream` call fails, that raises.

        Passes `head` explicitly to `--unset-upstream` rather than relying on
        the bare (HEAD-implicit) form: with a detached HEAD there is no
        current branch for git to infer, and the bare form raises
        "could not unset upstream of HEAD when it does not point to any
        branch" — `head` (from `_head_branch_name`) still resolves to a real
        branch even then.
        """
        with git.Repo(str(worktree.path)) as r:
            head = self._head_branch_name(r, worktree)
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
                r.git.branch("--unset-upstream", head)
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"unset_upstream failed for {worktree.repository.name}",
                    cwd=worktree.path,
                ) from exc

    def get_worktree_upstream(self, worktree: FeatureWorktree) -> str | None:
        """The worktree branch's current upstream (e.g. `origin/feature-123`), or None. No network.

        Returns None both when no upstream is configured AND when the configured
        upstream's remote-tracking ref doesn't resolve in the local object store
        — i.e. the worktree was connected (`winter ws connect` writes
        `branch.<head>.{remote,merge}` directly, without a matching remote ref)
        but origin has no such branch yet, or it was never fetched for this repo.
        gitpython's `tracking_branch()` returns the configured ref name even when
        it doesn't exist, so we verify resolution here. Without this, pulling an
        env where only some repos carry the feature branch would try to integrate
        a non-resolving ref in the others and mis-report it as divergence, failing
        the whole pull; treating it as "no upstream" lets those worktrees skip
        benignly (they have nothing to pull until the branch is pushed).
        """
        with git.Repo(str(worktree.path)) as r:
            name = self._tracking_branch_name(r)
            if name is None:
                return None
            return name if self._has_ref(r, name) else None

    def get_worktree_push_branch(self, worktree: FeatureWorktree) -> str | None:
        """The bare branch this worktree pushes to, read from its own tracking config.

        Returns the branch name from `branch.<head>.merge` when the worktree
        tracks `origin`, or None when no upstream is configured. Unlike
        `get_worktree_upstream` — which returns None for a connected feature
        branch whose remote ref doesn't resolve yet — this reads config directly
        via `read_origin_merge_branch`, so it still resolves the first-push
        target of a branch that origin doesn't have yet. No network.
        """
        with git.Repo(str(worktree.path)) as r:
            return read_origin_merge_branch(r, self._error_factory, cwd=worktree.path, label=worktree.repository.name)

    def set_push_default(self, worktree: FeatureWorktree) -> None:
        with git.Repo(str(worktree.path)) as r, r.config_writer() as cw:
            cw.set_value("push", "default", "upstream")

    def push(self, worktree: FeatureWorktree, feature_branch: str | None = None) -> int:
        message = f"push failed for {worktree.repository.name}"
        with git.Repo(str(worktree.path)) as r:
            if feature_branch:
                # Count commits in HEAD but not in origin/<feature_branch>.  On
                # the very first push to a new remote branch the remote ref
                # doesn't exist yet — rev-list against it would error, so fall
                # back to counting all commits in HEAD relative to the main
                # branch (what the remote will see as net-new).
                remote_ref = f"origin/{feature_branch}"
                try:
                    r.git.rev_parse("--verify", "--quiet", remote_ref)
                    remote_ref_exists = True
                except git.GitCommandError:
                    remote_ref_exists = False
                if remote_ref_exists:
                    commit_count = self._count_range(r, worktree.repository.name, f"{remote_ref}..HEAD")
                else:
                    main_ref = f"origin/{worktree.repository.main_branch}"
                    commit_count = self._count_range(r, worktree.repository.name, f"{main_ref}..HEAD")
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
                # Plain push — count commits ahead of the tracked upstream.
                try:
                    tracking = r.active_branch.tracking_branch()
                    tracking_name = tracking.name if tracking is not None else None
                except TypeError:
                    tracking_name = None
                if tracking_name is not None:
                    commit_count = self._count_range(r, worktree.repository.name, f"{tracking_name}..HEAD")
                else:
                    commit_count = 0
                self._git_ops.run_remote_git(
                    r,
                    "push",
                    "origin",
                    cwd=worktree.path,
                    message=message,
                )
        return commit_count

    def get_remote_branch_tip(self, worktree: FeatureWorktree, branch: str) -> str | None:
        """OID of `origin/<branch>` in the worktree's object store, or None when the
        remote ref doesn't resolve yet (first push of a new branch). No network.

        Read *before* a push to capture the remote tip the push will advance past,
        so `fast_forward_local_branch` can tell whether the workspace's local copy of
        the branch was in sync beforehand.
        """
        with git.Repo(str(worktree.path)) as r:
            try:
                return r.git.rev_parse("--verify", "--quiet", f"origin/{branch}")
            except git.GitCommandError:
                return None

    def fast_forward_local_branch(
        self, repo: ProjectRepository, branch: str, pre_remote_tip: str
    ) -> LocalFastForward | None:
        """Fast-forward the workspace's local `<branch>` to the just-pushed origin tip.

        Applies to *any* local branch of the pushed name — not just the repo's
        configured main — so an env connected to a shared integration branch keeps
        the workspace's copy of that branch in sync after a push. Guarded so it only
        ever advances a branch that was *exactly* at `pre_remote_tip` (the remote tip
        before the push): a local branch that is behind, ahead, or diverged is left
        for the user to integrate deliberately.

        Returns None when nothing applies (no local branch of that name, or already
        at the tip); a `LocalFastForward` describing the advance; or a skip with a
        reason when the branch was in sync but its checkout was dirty.

        The workspace's clones are linked git worktrees sharing one object store, so
        `origin/<branch>` here already reflects the push, and the local branch may be
        checked out in a *different* worktree than the one that pushed.
        """
        with git.Repo(str(repo.main_path)) as r:
            try:
                local_tip = r.git.rev_parse("--verify", "--quiet", f"refs/heads/{branch}")
            except git.GitCommandError:
                return None  # no local branch of this name — nothing to sync
            if local_tip != pre_remote_tip:
                return LocalFastForward(branch=branch, advanced=False, skipped_reason="not in sync")
            try:
                new_tip = r.git.rev_parse("--verify", "--quiet", f"refs/remotes/origin/{branch}")
            except git.GitCommandError:
                return None
            if new_tip == local_tip:
                return None
            checkout_path = self._worktree_checked_out_on(r, branch)

        # Not checked out anywhere: a pure ref advance is safe (we verified the
        # local branch is an ancestor of the new tip via local_tip == pre_remote_tip).
        if checkout_path is None:
            with git.Repo(str(repo.main_path)) as r:
                r.git.update_ref(f"refs/heads/{branch}", new_tip, local_tip)
            return LocalFastForward(branch=branch, advanced=True, commits=self._count_between(repo, local_tip, new_tip))

        # Checked out in some worktree: advance it there so the working tree and
        # index move with the ref. A dirty checkout is left untouched.
        with git.Repo(str(checkout_path)) as r:
            if r.is_dirty(working_tree=True, index=True, untracked_files=False):
                return LocalFastForward(branch=branch, advanced=False, skipped_reason="dirty")
            try:
                r.git.merge("--ff-only", f"origin/{branch}")
            except git.GitCommandError as exc:
                raise self._error_factory.from_git(
                    exc,
                    message=f"local fast-forward failed for {repo.name}",
                    cwd=checkout_path,
                ) from exc
            advanced = self._count_range(r, repo.name, f"{local_tip}..{r.head.commit.hexsha}")
            return LocalFastForward(branch=branch, advanced=True, commits=advanced)

    @staticmethod
    def _worktree_checked_out_on(r: git.Repo, branch: str) -> Path | None:
        """Path of the linked worktree that has `branch` checked out, or None.

        Parses `git worktree list --porcelain`; each record is a `worktree <path>`
        line optionally followed by a `branch refs/heads/<name>` line (absent for a
        detached HEAD).
        """
        current_path: Path | None = None
        for line in r.git.worktree("list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree ") :])
            elif line == f"branch refs/heads/{branch}":
                return current_path
        return None

    def _count_between(self, repo: ProjectRepository, old_tip: str, new_tip: str) -> int:
        with git.Repo(str(repo.main_path)) as r:
            return self._count_range(r, repo.name, f"{old_tip}..{new_tip}")

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

    def integrate_standalone_to_ref(
        self,
        repo: StandaloneRepository,
        target_ref: str,
        mode: PullMode,
        autostash: bool,
    ) -> RepoSyncOutcome:
        """Integrate a standalone repo against an explicit ref (e.g. ``origin/<branch>``).

        Used by the branch-pin pull path to ff-only advance a standalone to the
        current origin tip. Reuses the same ``_integrate`` machinery as
        ``integrate_standalone`` so divergence is refused (not force-reset) and
        ``autostash`` is honoured identically.
        """
        with git.Repo(str(repo.path)) as r:
            return self._integrate(r, repo.name, target_ref, mode, autostash)

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
                unwrap_gitpython_stream(exc.stderr).strip() if isinstance(exc.stderr, str) else exc,
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
                unwrap_gitpython_stream(exc.stderr).strip() if isinstance(exc.stderr, str) else exc,
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
                unwrap_gitpython_stream(exc.stderr).strip() if isinstance(exc.stderr, str) else exc,
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
                unwrap_gitpython_stream(exc.stderr).strip() if isinstance(exc.stderr, str) else exc,
            )

    @staticmethod
    def _head_branch_name(r: git.Repo, worktree: FeatureWorktree) -> str:
        """The branch name to key `branch.<name>.*` config writes off, tolerating a detached HEAD.

        `TypeError` on detached HEAD, `ValueError` on unborn HEAD — both mean
        there's no `r.active_branch` to read, the same condition the read
        paths already guard against (`branch_tracking.read_origin_merge_branch`,
        `ReadRepoRepository.get_standalone_status`). Those return `None` since
        a read can report "no answer"; a config write can't — it falls back to
        `worktree.environment.name`, the branch every non-pinned feature
        worktree is created under (`InitService._create_git_worktree`) and the
        one `force_checkout_env_branch` re-attaches a detached HEAD onto, so the fallback
        still names a real, git-visible branch.
        """
        try:
            return r.active_branch.name
        except (TypeError, ValueError):
            return worktree.environment.name

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
