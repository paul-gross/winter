from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class IWorkspaceRepository(Protocol):
    """Structural interface for any repo `winter ws init` reconciles.

    Both `ProjectRepository` and `StandaloneRepository` satisfy it. Used by helpers
    that don't care which kind of repo they're working with (writing git-excludes,
    running post-clone `cmd` lists, surfacing errors in the reporter).
    """

    name: str
    url: str | None
    git_excludes: list[str]
    cmd: list[str]


@dataclasses.dataclass
class Workspace:
    """The workspace as a whole — high-level attributes that span all environments and repositories."""

    root_path: Path
    session_prefix: str
    main_branch: str


@dataclasses.dataclass
class ProjectRepository:
    """A project repo that participates in feature environments (e.g. winter-app, winter-api).

    `name` doubles as the directory under `projects/` and as the user-facing label.
    It defaults to the trailing path segment of `url` (with `.git` stripped) when not
    explicitly set in the config, and can be overridden to give a clone a friendlier
    handle than its canonical repo name.
    """

    name: str
    main_path: Path
    main_branch: str
    pinned: bool = False
    url: str | None = None
    git_excludes: list[str] = dataclasses.field(default_factory=list)
    cmd: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class StandaloneRepository:
    """A repo that exists independently of feature environments.

    Covers both the implicit singletons (workspace, product, harness) and
    user-declared standalone repos (e.g. winter extensions). Singletons are
    discovered from the filesystem and only carry `name`/`path`; user-declared
    standalones come from `[[standalone_repository]]` in the workspace config
    and additionally carry `url`, `main_branch`, `git_excludes`, `cmd`, and an
    optional `prefix` that overrides the extension symlink prefix.
    """

    name: str
    path: Path
    main_branch: str | None = None
    url: str | None = None
    git_excludes: list[str] = dataclasses.field(default_factory=list)
    cmd: list[str] = dataclasses.field(default_factory=list)
    prefix: str | None = None


class DiffMode(enum.Enum):
    uncommitted = "uncommitted"
    staged = "staged"
    branch = "branch"


class RepoScope(enum.Enum):
    """Which kinds of repos a multi-repo command operates on.

    `project` is the default — feature-environment worktrees of project repos.
    `standalone` is just the user-declared standalone repos (unaffected by
    feature envs). `all` is both.
    """

    project = "project"
    standalone = "standalone"
    all = "all"

    @property
    def includes_project(self) -> bool:
        return self in (RepoScope.project, RepoScope.all)

    @property
    def includes_standalone(self) -> bool:
        return self in (RepoScope.standalone, RepoScope.all)


class PullMode(enum.Enum):
    """How `winter ws pull` integrates remote commits with the local branch.

    `ff_only` (default) refuses to integrate when the branch has diverged —
    no merge commits, no rewrites. `merge` falls back to a 3-way merge that
    creates a merge commit. `rebase` replays local commits onto the upstream.
    """

    ff_only = "ff_only"
    merge = "merge"
    rebase = "rebase"


class PinnedScope(enum.Enum):
    """Whether `winter ws push` includes pinned project worktrees.

    `exclude` (default) ignores pinned repos entirely — they track the main
    branch and are managed outside the feature-push flow. `include` pushes
    both pinned and non-pinned. `only` pushes pinned alone (useful when you've
    landed commits on a pinned repo's main branch and want to ship them).
    """

    exclude = "exclude"
    include = "include"
    only = "only"

    @property
    def matches_non_pinned(self) -> bool:
        return self in (PinnedScope.exclude, PinnedScope.include)

    @property
    def matches_pinned(self) -> bool:
        return self in (PinnedScope.include, PinnedScope.only)


class RepoError(Exception):
    """A repo operation failed. Wraps the underlying library exception.

    Raised by repository methods so callers can catch a single winter-defined
    type instead of depending on GitPython's exception hierarchy.

    Carries structured fields so the dashboard's Log tab can render a
    `git <subcommand> <args>` line plus cwd / exit code / stderr alongside the
    high-level message.
    """

    def __init__(
        self,
        message: str,
        *,
        subcommand: str | None = None,
        cmd_args: tuple[str, ...] = (),
        cwd: str | None = None,
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.subcommand = subcommand
        self.cmd_args = tuple(cmd_args)
        self.cwd = cwd
        self.exit_code = exit_code
        self.stderr = stderr

    def __str__(self) -> str:
        parts: list[str] = [self.message]
        if self.subcommand:
            cmd = " ".join(("git", self.subcommand, *self.cmd_args))
            parts.append(f"  $ {cmd}")
        if self.cwd:
            parts.append(f"  cwd: {self.cwd}")
        if self.exit_code is not None:
            parts.append(f"  exit {self.exit_code}")
        if self.stderr:
            parts.append(f"  stderr: {self.stderr.strip()}")
        return "\n".join(parts)


@dataclasses.dataclass
class FeatureEnvironment:
    """A named environment (alpha, beta, gamma) for feature development."""

    workspace: Workspace
    name: str
    index: int
    path: Path


@dataclasses.dataclass
class FeatureEnvironmentWorktrees:
    """All feature worktrees within an environment — used for bulk operations across repos."""

    environment: FeatureEnvironment
    worktrees: list[FeatureWorktree]


@dataclasses.dataclass
class FeatureWorktree:
    """A feature worktree — the intersection of an environment and a project repository."""

    workspace: Workspace
    environment: FeatureEnvironment
    repository: ProjectRepository

    @property
    def path(self) -> Path:
        return self.environment.path / self.repository.name
