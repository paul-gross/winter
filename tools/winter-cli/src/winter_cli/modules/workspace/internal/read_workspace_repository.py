from __future__ import annotations

import hashlib

import git

from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    FeatureEnvironmentStatus,
    ProjectRepository,
    Workspace,
)

GREEK_LETTERS = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi",
    "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
]

_GREEK_INDEX = {name: i + 1 for i, name in enumerate(GREEK_LETTERS)}
_NON_GREEK_OFFSET = 26


def resolve_env_index(name: str) -> int:
    """Map a worktree name to a port-offset index.

    Greek letters get fixed indices 1..24 so port assignments stay consistent
    across workspaces. Anything else is hashed deterministically into 26..281
    via SHA-1, leaving index 25 unused as a buffer between the two ranges.

    The 256-slot bucket size is bounded by the available port range — at 100
    ports per worktree and a typical usable range of ~28K ports, a higher
    ceiling would overflow what the OS can hand out. Collisions among
    non-Greek names exist but are negligible at the 1-3 concurrent ad-hoc
    worktrees a workspace typically runs.
    """
    if name in _GREEK_INDEX:
        return _GREEK_INDEX[name]
    digest = hashlib.sha1(name.encode()).digest()
    return _NON_GREEK_OFFSET + int.from_bytes(digest[:2], "big") % 256


class ReadWorkspaceRepository:
    """Read-only filesystem implementation of the workspace repository.

    Internal infrastructure — discovers feature environments by scanning the workspace root
    for Greek-letter directories and derives the connected feature branch from git's upstream
    tracking on the first non-pinned repo. Per-environment status badges are populated later
    by visual plugins (see `EnvironmentDecorator`); this class leaves `extensions={}` and
    has no awareness of any service-orchestration extension.
    """

    def __init__(self, error_factory: RepoErrorFactory) -> None:
        self._error_factory = error_factory

    def get_environments(self, workspace: Workspace, project_repos: list[ProjectRepository]) -> list[FeatureEnvironment]:
        return [self._build_environment(workspace, name) for name in self._discover_env_names(workspace, project_repos)]

    def get_environment(self, workspace: Workspace, name: str) -> FeatureEnvironment:
        return self._build_environment(workspace, name)

    def get_environment_status(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
    ) -> FeatureEnvironmentStatus:
        feature_branch = self._read_feature_branch(env, project_repos)
        return FeatureEnvironmentStatus(
            environment=env,
            feature_branch=feature_branch,
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

    def _read_feature_branch(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
    ) -> str | None:
        """Resolve the connected feature branch from each worktree's tracking config.

        The contract is: every non-pinned repo in a feature environment shares
        the same remote feature branch name, so we read it from the first
        non-pinned repo only. Pinned repos always track main and would lie.

        Reads `branch.<head>.{remote,merge}` directly via `git config` rather
        than `@{upstream}` — the latter requires the remote-tracking ref to
        exist locally, which it won't for a brand-new feature branch that's
        never been fetched. We want a freshly-connected env to read back as
        connected immediately.
        """
        # TypeError on detached HEAD and ValueError on unborn HEAD are both
        # "no feature branch yet", not failures.
        #
        # `git config --get` exits 1 specifically for "key not set" — that's
        # the "env not connected" answer. Any other exit code is a real
        # failure and raises so the dashboard's Log tab / CLI exit surface it.
        for repo in project_repos:
            if repo.pinned:
                continue
            worktree_path = env.path / repo.name
            if not (worktree_path / ".git").exists():
                return None
            try:
                r = git.Repo(str(worktree_path))
                head = r.active_branch.name
            except (TypeError, ValueError):
                return None
            try:
                remote = r.git.config("--get", f"branch.{head}.remote").strip()
                merge = r.git.config("--get", f"branch.{head}.merge").strip()
            except git.GitCommandError as exc:
                if exc.status == 1:
                    return None  # branch.<head>.{remote,merge} not configured
                raise self._error_factory.from_git(
                    exc,
                    message=f"reading feature-branch config failed for {repo.name}",
                    cwd=worktree_path,
                ) from exc
            if remote != "origin" or not merge.startswith("refs/heads/"):
                return None
            return merge[len("refs/heads/"):]
        return None
