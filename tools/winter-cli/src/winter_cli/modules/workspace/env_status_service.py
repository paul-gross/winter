from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import click

from winter_cli.modules.workspace.models import (
    DiffMode,
    EnvDiffResult,
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    RepoDiffResult,
    RepoError,
    WorktreeRepoStatus,
)
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.plugins.types import IEnvironmentDecorator, IWorktreeRepoDecorator

logger = logging.getLogger(__name__)

# Fan-out width for the per-worktree status reads in `get_worktree_repo_statuses`.
# These are *local* git reads (each opens its own repo and shells out), so —
# unlike the SSH-capped remote ops bounded by `GitOpsService.PARALLELISM` — they
# are not throttled by the host and a wider pool just maps onto more concurrent
# `git` subprocesses. Bounded so a workspace with many repos doesn't spawn an
# unbounded subprocess herd.
STATUS_PARALLELISM: int = 8


class EnvStatusService:
    """Read-only environment and worktree status for the dashboard and JSON output.

    Builds `FeatureEnvironmentWorktrees` from project repos, surfaces per-worktree
    git status (branch, ahead/behind, dirty, tracking divergence), and computes
    per-env diffs. All public methods are side-effect free except for emitting
    callbacks to decorator plugins.
    """

    def __init__(
        self,
        worktree_repo: IReadWorkspaceRepository,
        repo_repo: IWriteRepoRepository,
    ) -> None:
        self._worktree_repo = worktree_repo
        self._repo_repo = repo_repo

    def get_environment_status(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
        env_decorators: list[IEnvironmentDecorator] | None = None,
    ) -> FeatureEnvironmentStatus:
        """Read the env's git-tracked status and let visual plugins decorate it.

        Plugins receive the freshly-built `FeatureEnvironmentStatus` and the env's
        worktree path, and may write into `status.extensions` to surface a badge
        in the dashboard column header. Pass `env_decorators=None` (default) when
        you don't want decoration — e.g. headless `winter ws status` JSON output.
        """
        status = self._worktree_repo.get_environment_status(env, project_repos)
        if env_decorators:
            for decorator in env_decorators:
                try:
                    decorator(status, env.path)
                except Exception:
                    logger.warning("environment decorator failed", exc_info=True)
        return status

    def get_worktree_repo_statuses(
        self,
        env_worktrees: FeatureEnvironmentWorktrees,
        worktree_repo_decorators: list[IWorktreeRepoDecorator] | None = None,
        on_repo_error: Callable[[FeatureWorktree, RepoError], None] | None = None,
    ) -> list[WorktreeRepoStatus]:
        """Read one row per worktree, optionally tolerating per-worktree failures.

        When `on_repo_error` is `None` (CLI / JSON output / tests), the first
        `RepoError` propagates so the caller exits non-zero with full context.
        When the dashboard passes a callback, the failed worktree is reported
        to it and skipped — the rest of the env still renders. This is what
        keeps one broken repo from hanging the whole dashboard refresh.
        """
        env = env_worktrees.environment
        worktrees = env_worktrees.worktrees

        # Fan the per-worktree status reads out across a bounded thread pool —
        # each `get_worktree_status` opens its own git repo and shells out to
        # `git`, so the work is subprocess/IO-bound and threads parallelize it
        # without contending on the GIL. Futures are collected back in worktree
        # order, and exceptions are re-raised at `.result()` time, so both the
        # output ordering and the error semantics below are identical to the
        # former serial loop (the first failing worktree in order propagates,
        # or is reported and skipped, exactly as before).
        wt_repo_statuses: list[WorktreeRepoStatus] = []
        if worktrees:
            with ThreadPoolExecutor(max_workers=min(len(worktrees), STATUS_PARALLELISM)) as pool:
                futures = [pool.submit(self._repo_repo.get_worktree_status, wt) for wt in worktrees]
            for wt, future in zip(worktrees, futures, strict=True):
                try:
                    rs = future.result()
                except RepoError as exc:
                    if on_repo_error is None:
                        raise
                    on_repo_error(wt, exc)
                    continue
                wt_repo_statuses.append(
                    WorktreeRepoStatus(
                        worktree=wt,
                        branch=rs.branch,
                        ahead=rs.ahead,
                        behind=rs.behind,
                        dirty_count=len(rs.dirty_files),
                        tracking_branch=rs.tracking_branch,
                        tracking_ahead=rs.tracking_ahead,
                        tracking_behind=rs.tracking_behind,
                        tracking_ref_present=rs.tracking_ref_present,
                    )
                )

        if worktree_repo_decorators:
            for decorator in worktree_repo_decorators:
                for wt_repo_status in wt_repo_statuses:
                    repo_path = env.path / wt_repo_status.worktree.repository.name
                    decorator(wt_repo_status, repo_path)

        return wt_repo_statuses

    def get_feature_environment_worktrees(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
    ) -> FeatureEnvironmentWorktrees:
        worktrees = [
            FeatureWorktree(workspace=env.workspace, environment=env, repository=repo) for repo in project_repos
        ]
        return FeatureEnvironmentWorktrees(environment=env, worktrees=worktrees)

    def get_feature_worktree(self, env: FeatureEnvironment, repo: ProjectRepository) -> FeatureWorktree:
        return FeatureWorktree(workspace=env.workspace, environment=env, repository=repo)

    def get_env_diff(
        self,
        env_worktrees: FeatureEnvironmentWorktrees,
        mode: DiffMode,
        repo_filter: str | None = None,
    ) -> EnvDiffResult:
        worktrees = env_worktrees.worktrees

        if repo_filter:
            matched = [wt for wt in worktrees if repo_filter == wt.repository.name]
            if not matched:
                raise click.ClickException(f"Repo '{repo_filter}' not found")
            worktrees = matched

        results: list[RepoDiffResult] = []
        for wt in worktrees:
            diff = self._repo_repo.get_diff(wt, mode)
            if not diff.diff_text:
                continue
            if mode == DiffMode.branch and wt.repository.pinned and diff.ahead == 0:
                continue
            results.append(diff)

        return EnvDiffResult(env=env_worktrees.environment.name, mode=mode, repos=results)
