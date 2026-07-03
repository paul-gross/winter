from __future__ import annotations

import logging

from winter_cli.modules.workspace.models import (
    CheckoutResult,
    EnvCheckoutReport,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    RepoCheckoutOutcome,
)
from winter_cli.modules.workspace.pattern_match import matches_any_pattern
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository

logger = logging.getLogger(__name__)


class EnvCheckoutService:
    """Connect / disconnect / checkout for the feature branch of an env's worktrees.

    `connect_env` and `disconnect_env` wire (or unwire) the per-worktree upstream
    tracking. `checkout_env` is the two-phase adoption of a remote feature branch
    into every non-pinned worktree in the env. Phase 1 is a non-destructive
    safety check that aborts the entire run if any repo refuses (so a refusal
    blocks Phase 2 globally — no connect and no `git reset` executes in that
    case). Phase 2 then runs the destructive `set_upstream` / `set_push_default`
    / `hard_reset` sequence serially across every non-pinned repo; if a Phase 2
    git op raises mid-loop, earlier repos have already been mutated and the
    exception propagates with no rollback.
    """

    def __init__(self, repo_repo: IWriteRepoRepository) -> None:
        self._repo_repo = repo_repo

    def connect_env(
        self,
        env_worktrees: FeatureEnvironmentWorktrees,
        feature_branch: str,
        patterns: list[str],
    ) -> list[str]:
        """Connect every non-pinned worktree matching `patterns` to `origin/<feature_branch>`.

        `patterns` are segment-aware `<env>/<repo>` globs (a bare `<env>`
        matches `<env>/*`), so a whole-env connect and a single-worktree
        connect go through the same path — the bare-env form is just the
        all-matching case. Pinned worktrees are always skipped. Returns the
        names of the worktrees that were connected, in env order.
        """
        env_name = env_worktrees.environment.name
        logger.info("connect_env: env=%s feature_branch=%s patterns=%s", env_name, feature_branch, patterns)
        connected: list[str] = []
        for wt in env_worktrees.worktrees:
            if wt.repository.pinned:
                continue
            if not matches_any_pattern(env_name, wt.repository.name, patterns):
                continue
            self._repo_repo.set_upstream(wt, f"origin/{feature_branch}")
            self._repo_repo.set_push_default(wt)
            connected.append(wt.repository.name)
        return connected

    def disconnect_env(self, env_worktrees: FeatureEnvironmentWorktrees, patterns: list[str]) -> list[str]:
        """Disconnect every non-pinned worktree matching `patterns` from its feature branch.

        `patterns` are segment-aware `<env>/<repo>` globs (a bare `<env>`
        matches `<env>/*`), the same grammar as `connect_env`. Pinned
        worktrees are always skipped. Returns the names of the worktrees
        that were disconnected, in env order.
        """
        env_name = env_worktrees.environment.name
        logger.info("disconnect_env: env=%s patterns=%s", env_name, patterns)
        disconnected: list[str] = []
        for wt in env_worktrees.worktrees:
            if wt.repository.pinned:
                continue
            if not matches_any_pattern(env_name, wt.repository.name, patterns):
                continue
            self._repo_repo.unset_upstream(wt)
            disconnected.append(wt.repository.name)
        return disconnected

    def checkout_env(
        self,
        env_worktrees: FeatureEnvironmentWorktrees,
        feature_branch: str,
        force: bool,
        new: bool = False,
    ) -> EnvCheckoutReport:
        """Adopt `origin/<feature_branch>` into every non-pinned worktree repo.

        No network — operates on local refs (run `winter ws fetch` first for
        fresh ones). Phase 1 classifies each repo locally. Two ref-resolution
        guards run regardless of `force`: a feature ref that resolves in *no*
        repo refuses unless `new` is set (a branch the local store has never
        seen is more likely a typo or a missing fetch than a deliberate new
        branch), and a repo where neither the feature ref nor
        `origin/<main_branch>` resolves refuses (Phase 2 would have nothing to
        reset it to). The safety classification — dirty, or *abandonment*
        (HEAD carries commits not on the branch the env is moving away from —
        its own current upstream) — is skipped under `force`. If any repo
        refuses, Phase 2 is skipped — no connect, no reset anywhere.
        Otherwise Phase 2 connects every non-pinned repo to
        `origin/<feature_branch>` and hard-resets it to that ref where it
        exists, or to the repo's `origin/<main_branch>` where the feature ref
        is absent (a new branch started from main, created on first push).

        Phase 2 is not atomic across repos: if a git op raises mid-loop, repos
        processed earlier are already mutated and the exception propagates
        with no rollback. Callers that need a clean restart must capture the
        per-repo HEADs before calling and reset manually.
        """
        logger.info(
            "checkout_env: env=%s feature_branch=%s force=%s new=%s",
            env_worktrees.environment.name,
            feature_branch,
            force,
            new,
        )
        feature_ref = f"origin/{feature_branch}"
        targets = [wt for wt in env_worktrees.worktrees if not wt.repository.pinned]
        have_feature_ref = {wt.repository.name: self._repo_repo.has_local_ref(wt, feature_ref) for wt in targets}

        refused: list[RepoCheckoutOutcome] = []
        if targets and not new and not any(have_feature_ref.values()):
            # Phase 1 — ref-resolution guard (local, no network; not bypassed
            # by --force): the feature ref resolves nowhere, which is more
            # likely a typo or a missing `winter ws fetch` than a new branch.
            # --new is the explicit opt-in for starting one from main.
            refused = [RepoCheckoutOutcome(wt.repository.name, CheckoutResult.refused_unknown_branch) for wt in targets]
        else:
            # Ref-resolution guard, per repo (also not bypassed by --force): a
            # repo where neither the feature ref nor origin/<main> resolves
            # has nothing for Phase 2 to reset to — refusing up front beats
            # raising mid-loop after earlier repos were already mutated.
            for wt in targets:
                if not have_feature_ref[wt.repository.name] and not self._repo_repo.has_local_ref(
                    wt, f"origin/{wt.repository.main_branch}"
                ):
                    refused.append(RepoCheckoutOutcome(wt.repository.name, CheckoutResult.refused_missing_ref))
            refused_names = {o.repo_name for o in refused}

            # Phase 1 — safety classification (local, no network). Skipped under
            # --force. Compares against each repo's *own* upstream, not the target.
            if not force:
                for wt in targets:
                    if wt.repository.name in refused_names:
                        continue
                    if self._repo_repo.is_worktree_dirty(wt):
                        refused.append(RepoCheckoutOutcome(wt.repository.name, CheckoutResult.refused_dirty))
                        continue
                    safety_ref = self._abandonment_safety_ref(wt)
                    if self._repo_repo.count_commits_not_in(wt, safety_ref) > 0:
                        refused.append(RepoCheckoutOutcome(wt.repository.name, CheckoutResult.refused_abandonment))

        if refused:
            logger.warning(
                "checkout_env: aborting — refused repos: %s",
                ", ".join(o.repo_name for o in refused),
            )
            return EnvCheckoutReport(
                env=env_worktrees.environment.name,
                feature_branch=feature_branch,
                aborted=True,
                repos=refused,
            )

        # Phase 2 — reset to the feature ref (or main) first, then connect.
        # hard_reset runs before set_upstream / set_push_default so that a
        # mid-loop failure leaves the worktree on its original tracking rather
        # than re-pointed at the new upstream but stuck on the old HEAD.
        outcomes: list[RepoCheckoutOutcome] = []
        for wt in targets:
            if have_feature_ref[wt.repository.name]:
                self._repo_repo.hard_reset(wt, feature_ref)
                self._repo_repo.set_upstream(wt, feature_ref)
                self._repo_repo.set_push_default(wt)
                outcomes.append(RepoCheckoutOutcome(wt.repository.name, CheckoutResult.reset_feature))
            else:
                self._repo_repo.hard_reset(wt, f"origin/{wt.repository.main_branch}")
                self._repo_repo.set_upstream(wt, feature_ref)
                self._repo_repo.set_push_default(wt)
                outcomes.append(RepoCheckoutOutcome(wt.repository.name, CheckoutResult.reset_main))

        return EnvCheckoutReport(
            env=env_worktrees.environment.name,
            feature_branch=feature_branch,
            aborted=False,
            repos=outcomes,
        )

    def _abandonment_safety_ref(self, wt: FeatureWorktree) -> str:
        """The ref a checkout would abandon work relative to.

        The worktree's own current upstream when it resolves locally, else the
        repo's `origin/<main_branch>`. Comparing against the branch the env is
        moving *away from* (not the target) is what makes the guard protect
        unpushed local commits. The fallback covers a disconnected env or a
        never-pushed upstream whose ref isn't in the local object store.
        """
        upstream = self._repo_repo.get_worktree_upstream(wt)
        if upstream is not None and self._repo_repo.has_local_ref(wt, upstream):
            return upstream
        return f"origin/{wt.repository.main_branch}"
