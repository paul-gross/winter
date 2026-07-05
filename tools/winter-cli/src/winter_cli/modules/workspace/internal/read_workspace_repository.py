from __future__ import annotations

from pathlib import Path

import git

from winter_cli.modules.workspace.env_index import GREEK_LETTERS, resolve_env_index
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry
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

    Internal infrastructure — discovers feature environments from the env-index registry's
    recorded names unioned with the built-in Greek aliases, each confirmed on disk, and derives
    the connected feature branch from git's upstream
    tracking on the first connected non-pinned repo (plus a count of how many distinct remote
    branches the env's worktrees span, for the dashboard's multi-remote badge). Per-environment
    status badges are populated later by visual plugins (see `IEnvironmentDecorator`); this class
    leaves `extensions={}` and has no awareness of any service-orchestration extension.
    """

    def __init__(
        self,
        error_factory: RepoErrorFactory,
        env_aliases: list[str] | None = None,
        envs_per_workspace: int | None = None,
        registry: IEnvIndexRegistry | None = None,
    ) -> None:
        self._error_factory = error_factory
        self._env_aliases = env_aliases
        self._envs_per_workspace = envs_per_workspace
        self._registry = registry

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
        for name in self._candidate_env_names():
            candidate = workspace.root_path / name
            if not candidate.is_dir():
                continue
            subdirs = {d.name for d in candidate.iterdir() if d.is_dir()}
            if subdirs & known_repos:
                found.append(name)
        # Order the dashboard by env index so aliases (1..N) lead and non-alias
        # envs follow in their allocated-slot order, regardless of the arbitrary
        # order candidates were gathered in.
        return sorted(found, key=self._resolve_index)

    def _candidate_env_names(self) -> list[str]:
        """Env names to probe on disk, unioned from two sources.

        * The index registry — the authoritative list of every env
          ``reconcile_env`` has allocated, and the *only* place a non-alias name
          like ``feature-xyz`` is recorded. Discovery must consult it, or
          arbitrarily-named envs never appear on the dashboard.
        * The configured env aliases (``self._env_aliases``, defaulting to the
          built-in ``GREEK_LETTERS`` when unset) — kept so a pre-registry env on
          disk that predates ``state.toml`` is still discovered. This is the same
          alias source ``_resolve_index`` falls back to, so discovery and
          index-resolution agree even in a workspace with non-Greek aliases.

        Each candidate is still confirmed against the filesystem by the caller
        (directory exists and holds a known project-repo worktree). That guard is
        why we can't just scan the workspace root: ``projects/`` holds the source
        checkouts, named identically to the repos, and would false-positive as an
        env.
        """
        aliases = self._env_aliases if self._env_aliases is not None else GREEK_LETTERS
        registered = list(self._registry.all_assignments()) if self._registry is not None else []
        return list(dict.fromkeys([*aliases, *registered]))

    def _build_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        path = workspace.root_path / name
        index = self._resolve_index(name)
        return FeatureEnvironment(
            workspace=workspace,
            name=name,
            index=index,
            path=path,
        )

    def _resolve_index(self, name: str) -> int:
        """Return the env index for *name*.

        Checks the registry first (returns the persisted assignment when present).
        Falls back to ``resolve_env_index`` for pre-registry envs that have no
        recorded entry (created before the registry existed).
        """
        if self._registry is not None:
            recorded = self._registry.get_index(name)
            if recorded is not None:
                return recorded
        return resolve_env_index(name, self._env_aliases, self._envs_per_workspace)

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
