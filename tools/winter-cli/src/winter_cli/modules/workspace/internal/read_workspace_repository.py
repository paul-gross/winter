from __future__ import annotations

from pathlib import Path

import git

from winter_cli.modules.workspace.env_index import GREEK_LETTERS, resolve_env_index
from winter_cli.modules.workspace.internal.branch_tracking import read_origin_merge_branch
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    ProjectRepository,
    Workspace,
)
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository


class ReadWorkspaceRepository:
    """Read-only filesystem implementation of the workspace repository.

    Internal infrastructure — discovers feature environments by scanning the workspace root
    for Greek-letter directories and derives the connected feature branch from git's upstream
    tracking on the first connected non-pinned repo (plus a count of how many distinct remote
    branches the env's worktrees span, for the dashboard's multi-remote badge). Per-environment
    status badges are populated later by visual plugins (see `IEnvironmentDecorator`); this class
    leaves `extensions={}` and has no awareness of any service-orchestration extension.
    """

    def __init__(self, error_factory: RepoErrorFactory) -> None:
        self._error_factory = error_factory

    def get_environments(
        self, workspace: Workspace, project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        return [self._build_environment(workspace, name) for name in self._discover_env_names(workspace, project_repos)]

    def get_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        return self._build_environment(workspace, name)

    def get_environment_status(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
    ) -> FeatureEnvironmentStatus:
        branches = self._read_feature_branches(env, project_repos)
        # `feature_branch` is the env's primary — the first *connected* non-pinned
        # worktree's branch (a disconnected leading repo is skipped, so the
        # primary is the first repo that actually tracks a feature branch).
        # `distinct_remote_count` is how many distinct remote branches the env's
        # worktrees point at (inclusive of the primary), so the dashboard can
        # flag a multi-remote env as `feature-x+N` where N = distinct_remote_count - 1.
        feature_branch = next((b for b in branches if b is not None), None)
        distinct_remote_count = len({b for b in branches if b is not None})
        return FeatureEnvironmentStatus(
            environment=env,
            feature_branch=feature_branch,
            distinct_remote_count=distinct_remote_count,
        )

    def _discover_env_names(self, workspace: Workspace, project_repos: list[ProjectRepository]) -> list[str]:
        known_repos = {r.name for r in project_repos}
        found = []
        for name in GREEK_LETTERS:
            candidate = workspace.root_path / name
            if not candidate.is_dir():
                continue
            subdirs = {d.name for d in candidate.iterdir() if d.is_dir()}
            if subdirs & known_repos:
                found.append(name)
        return found

    def _build_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        path = workspace.root_path / name
        return FeatureEnvironment(
            workspace=workspace,
            name=name,
            index=resolve_env_index(name),
            path=path,
        )

    def _read_feature_branches(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
    ) -> list[str | None]:
        """The configured feature branch of each non-pinned worktree, in repo order.

        Pinned repos always track main and would lie, so they're excluded. Each
        entry is the worktree's remote feature branch, or `None` when it isn't
        connected to one (disconnected, detached/unborn HEAD, or a missing
        worktree). `get_environment_status` takes the first non-`None` entry as
        the env's primary `feature_branch`; the full list lets it count how many
        *distinct* remote branches the env's worktrees span.
        """
        branches: list[str | None] = []
        for repo in project_repos:
            if repo.pinned:
                continue
            branches.append(self._read_worktree_feature_branch(env.path / repo.name, repo.name))
        return branches

    def _read_worktree_feature_branch(self, worktree_path: Path, repo_name: str) -> str | None:
        """One worktree's connected feature branch, or `None` when not connected.

        Delegates to `read_origin_merge_branch`, which reads
        `branch.<head>.{remote,merge}` config directly so a freshly-connected,
        never-fetched worktree reads back as connected immediately.
        """
        if not (worktree_path / ".git").exists():
            return None
        with git.Repo(str(worktree_path)) as r:
            return read_origin_merge_branch(r, self._error_factory, cwd=worktree_path, label=repo_name)


def _conforms_read_workspace_repository(x: ReadWorkspaceRepository) -> IReadWorkspaceRepository:
    return x
