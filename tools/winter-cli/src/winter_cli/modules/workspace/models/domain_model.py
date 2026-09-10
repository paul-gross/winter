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
    service_prefix: str
    main_branch: str
    base_port: int = 4000
    """Start of this workspace's port band. Per-env port base = base_port + index * ports_per_env."""

    ports_per_env: int = 20
    """Number of ports allocated per feature environment."""

    def port_base_for(self, index: int) -> int:
        """Return the per-env port base for the given env index.

        Canonical single definition: ``base_port + index * ports_per_env``.
        """
        return self.base_port + index * self.ports_per_env


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
    main_branch: str | None
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
    and additionally carry `url`, `main_branch`, `git_excludes`, `cmd`, an
    optional `prefix` that overrides the extension symlink prefix, and an
    optional `ref` (branch, tag, or commit) that pins the checkout.

    ``ref`` semantics:
    - absent → clone tracks the default branch; pull integrates tracked upstream.
    - branch ref → checkout on that tracking branch; pull fast-forwards it
      (moving pin).
    - tag / commit ref → detached checkout held exactly at the resolved commit;
      pull never advances it (frozen pin).

    ``ref`` is distinct from ``main_branch`` (the integration tracking target)
    and entirely unrelated to ``ProjectRepository.pinned`` (which means "exclude
    from feature branching" — a project-repo-only concept).
    """

    name: str
    path: Path
    main_branch: str | None = None
    url: str | None = None
    git_excludes: list[str] = dataclasses.field(default_factory=list)
    cmd: list[str] = dataclasses.field(default_factory=list)
    prefix: str | None = None
    ref: str | None = None
    config_dir: Path | None = None


class RefKind(enum.StrEnum):
    """How a standalone repo's ``ref`` field should be interpreted.

    Matches the ``kind`` field recorded in ``.winter/config.lock`` entries.
    """

    branch = "branch"
    tag = "tag"
    commit = "commit"


@dataclasses.dataclass(frozen=True)
class LockEntry:
    """A resolved pin for one standalone repo, persisted in ``.winter/config.lock``.

    ``name`` matches the ``[[standalone_repository]].name`` in the config.
    ``ref`` is the intent string copied verbatim from the config (used for
    drift detection: a mismatch between config ref and lock ref means the
    lock is stale).
    ``kind`` classifies the ref as a branch, tag, or raw commit SHA.
    ``commit`` is the full 40-character SHA that was resolved at lock time.
    """

    name: str
    ref: str
    kind: RefKind
    commit: str


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


class MergeMode(enum.Enum):
    """How `winter ws merge` integrates the source ref into the worktree's branch.

    Mirrors `PullMode` with `--no-ff` added in place of `--rebase` (rebase
    against an arbitrary source ref isn't a meaningful merge operation).

    `ff_only` (default) refuses to integrate when the branches have
    diverged — never produces a merge commit. `merge` falls back to a
    3-way merge commit when ff-only fails. `no_ff` always creates a
    merge commit even when fast-forward is possible (mirrors
    `git merge --no-ff`).
    """

    ff_only = "ff_only"
    merge = "merge"
    no_ff = "no_ff"


class ResetMode(enum.Enum):
    """How `winter ws reset` moves a matched worktree's branch pointer.

    Mirrors git's own three-tree `reset` semantics exactly. `soft` moves only
    the branch pointer — the index and working tree are untouched, so the
    delta between the old and new position lands staged. `mixed` (default)
    additionally resets the index (working tree still untouched), so the
    delta lands unstaged. `hard` resets all three trees, discarding the
    delta entirely (the only mode the dirty / abandonment safety gate
    applies to). None of the three touch upstream tracking — `reset` moves
    the pointer, `winter ws connect` changes tracking, and the two never mix.
    """

    soft = "soft"
    mixed = "mixed"
    hard = "hard"


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
    `<program> <subcommand> <args>` line plus cwd / exit code / stderr
    alongside the high-level message. `program` defaults to `"git"` — every
    wrap site that predates non-git adapters (i.e. every `from_git` call)
    leaves it unset and renders exactly as before; a non-git adapter (e.g.
    `RepoErrorFactory.from_subprocess`) passes the program it actually ran so
    the rendered command line names it instead.
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
        program: str = "git",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.subcommand = subcommand
        self.cmd_args = tuple(cmd_args)
        self.cwd = cwd
        self.exit_code = exit_code
        self.stderr = stderr
        self.program = program

    def __str__(self) -> str:
        parts: list[str] = [self.message]
        if self.subcommand:
            cmd = " ".join((self.program, self.subcommand, *self.cmd_args))
            parts.append(f"  $ {cmd}")
        if self.cwd:
            parts.append(f"  cwd: {self.cwd}")
        if self.exit_code is not None:
            if self.exit_code < 0:
                parts.append(f"  exit {self.exit_code} (killed by signal {-self.exit_code})")
            else:
                parts.append(f"  exit {self.exit_code}")
        if self.stderr:
            parts.append(f"  stderr: {self.stderr.strip()}")
        return "\n".join(parts)


class PartialCleanError(RepoError):
    """`git clean` failed *after* already deleting some paths.

    `git clean -fd` is not transactional: it removes what it can, warns on
    what it cannot (an unreadable directory, a permission error), and exits
    non-zero having already deleted files. Those deletions are unrecoverable,
    so the paths git named on stdout before failing are the only record they
    ever existed — a plain `RepoError` would carry stderr and drop them.

    Subclasses `RepoError` so existing `except RepoError` handlers keep
    working; callers that want the partial record opt in by catching this.
    """

    def __init__(self, message: str, *, removed: list[str], **kwargs: object) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.removed = list(removed)


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
