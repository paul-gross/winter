from __future__ import annotations

import logging

from winter_cli.modules.workspace.models import (
    CheckoutResult,
    EnvCheckoutReport,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    RepoCheckoutOutcome,
)
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository

logger = logging.getLogger(__name__)


class EnvCheckoutService:
    """Connect / disconnect / checkout for the feature branch of an env's worktrees.

    `connect_env` and `disconnect_env` wire (or unwire) the per-worktree upstream
    tracking. `checkout_env` is the two-phase adoption of a remote feature branch
    into every non-pinned worktree in the env. Phase 1 is a non-destructive
    safety check that aborts the entire run if any repo refuses (so a refusal
    blocks Phase 2 globally — no `git reset` executes in that case). Phase 2
    then runs the destructive `set_upstream` / `set_push_default` / `hard_reset`
    sequence serially across the passing repos; if a Phase 2 git op raises
    mid-loop, earlier repos have already been mutated and the exception
    propagates with no rollback.
    """

    def __init__(self, repo_repo: IWriteRepoRepository) -> None:
        self._repo_repo = repo_repo

    def connect_env(self, env_worktrees: FeatureEnvironmentWorktrees, feature_branch: str) -> int:
        logger.info("connect_env: env=%s feature_branch=%s", env_worktrees.environment.name, feature_branch)
        count = 0
        for wt in env_worktrees.worktrees:
            if wt.repository.pinned:
                continue
            self._repo_repo.set_upstream(wt, f"origin/{feature_branch}")
            self._repo_repo.set_push_default(wt)
            count += 1
        return count

    def disconnect_env(self, env_worktrees: FeatureEnvironmentWorktrees) -> int:
        logger.info("disconnect_env: env=%s", env_worktrees.environment.name)
        count = 0
        for wt in env_worktrees.worktrees:
            if wt.repository.pinned:
                continue
            self._repo_repo.unset_upstream(wt)
            count += 1
        return count

    def checkout_env(
        self,
        env_worktrees: FeatureEnvironmentWorktrees,
        feature_branch: str,
        force: bool,
    ) -> EnvCheckoutReport:
        """Adopt `origin/<feature_branch>` into every non-pinned worktree repo.

        Phase 1 classifies each repo locally (no network): dirty / divergent
        / missing-ref / clean. If any repo refuses safety in non-force mode,
        Phase 2 is skipped — `git reset --hard` runs in no repo. Otherwise
        Phase 2 wires upstream tracking and resets the Greek-letter branch to
        the local `origin/<feature_branch>` ref in each repo that has it.

        Phase 2 is not atomic across repos: if a git op raises mid-loop, repos
        processed earlier are already mutated and the exception propagates
        with no rollback. Callers that need a clean restart must capture the
        per-repo HEADs before calling and reset manually.
        """
        logger.info(
            "checkout_env: env=%s feature_branch=%s force=%s",
            env_worktrees.environment.name,
            feature_branch,
            force,
        )
        remote_ref = f"origin/{feature_branch}"
        targets = [wt for wt in env_worktrees.worktrees if not wt.repository.pinned]

        passing: list[FeatureWorktree] = []
        refused: list[RepoCheckoutOutcome] = []
        skipped: list[RepoCheckoutOutcome] = []
        for wt in targets:
            if not self._repo_repo.has_local_ref(wt, remote_ref):
                skipped.append(
                    RepoCheckoutOutcome(
                        repo_name=wt.repository.name,
                        result=CheckoutResult.skip_missing_ref,
                    )
                )
                continue
            if not force:
                if self._repo_repo.is_worktree_dirty(wt):
                    refused.append(
                        RepoCheckoutOutcome(
                            repo_name=wt.repository.name,
                            result=CheckoutResult.refused_dirty,
                        )
                    )
                    continue
                if self._repo_repo.count_commits_not_in(wt, remote_ref) > 0:
                    refused.append(
                        RepoCheckoutOutcome(
                            repo_name=wt.repository.name,
                            result=CheckoutResult.refused_divergent,
                        )
                    )
                    continue
            passing.append(wt)

        if refused:
            logger.warning(
                "checkout_env: aborting — refused repos: %s",
                ", ".join(o.repo_name for o in refused),
            )
            return EnvCheckoutReport(
                env=env_worktrees.environment.name,
                feature_branch=feature_branch,
                aborted=True,
                repos=refused + skipped,
            )

        applied: list[RepoCheckoutOutcome] = []
        for wt in passing:
            self._repo_repo.set_upstream(wt, remote_ref)
            self._repo_repo.set_push_default(wt)
            self._repo_repo.hard_reset(wt, remote_ref)
            applied.append(
                RepoCheckoutOutcome(
                    repo_name=wt.repository.name,
                    result=CheckoutResult.reset,
                )
            )

        repo_order = [wt.repository.name for wt in targets]
        outcomes = applied + skipped
        outcomes.sort(key=lambda o: repo_order.index(o.repo_name))
        return EnvCheckoutReport(
            env=env_worktrees.environment.name,
            feature_branch=feature_branch,
            aborted=False,
            repos=outcomes,
        )
