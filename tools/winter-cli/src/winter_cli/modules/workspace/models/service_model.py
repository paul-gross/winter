from __future__ import annotations

import dataclasses
import enum
from typing import Any, Protocol, runtime_checkable

from winter_cli.modules.workspace.models.domain_model import (
    DiffMode,
    FeatureEnvironment,
    FeatureWorktree,
    StandaloneRepository,
)


@runtime_checkable
class IRepoStatus(Protocol):
    @property
    def name(self) -> str: ...

    branch: str | None
    ahead: int
    behind: int
    dirty_count: int
    tracking_ahead: int
    tracking_behind: int


@dataclasses.dataclass
class RepoCommit:
    """A single commit on a branch — abbreviated hash and first line of the message."""

    short_hash: str
    message: str


@dataclasses.dataclass
class RepoStatus:
    """Detailed git status of a single repository — branch, ahead/behind, dirty files, and recent commits."""

    name: str
    path: str
    main_branch: str | None
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_files: list[str] = dataclasses.field(default_factory=list)
    recent_commits: list[RepoCommit] = dataclasses.field(default_factory=list)
    tracking_branch: str | None = None
    tracking_ahead: int = 0
    tracking_behind: int = 0
    tracking_ref_present: bool = False
    """Whether the remote-tracking ref actually resolves locally.

    Distinguishes "upstream configured but never fetched / never pushed"
    (False) from "upstream configured and up-to-date" (True with
    tracking_ahead == 0). Without this, both states read as tracking_ahead=0
    because git rev-list silently returns 0 when the ref is missing.
    """


@dataclasses.dataclass
class StandaloneRepoStatus:
    """Lightweight status for standalone repositories (workspace, product, harness)."""

    repository: StandaloneRepository
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_count: int = 0
    tracking_ahead: int = 0
    tracking_ref_present: bool = False
    latest_commit: str | None = None

    @property
    def name(self) -> str:
        return self.repository.name


@dataclasses.dataclass
class FeatureEnvironmentStatus:
    """Runtime status of a feature environment — feature branch plus extension-contributed badges.

    `extensions` is keyed by extension prefix (e.g. `wst` for winter-service-tmux); each value
    is a short badge string an `EnvironmentDecorator` plugin contributed for this env. Renderers
    append the values to the env header so each plugin can advertise whatever it wants.
    """

    environment: FeatureEnvironment
    feature_branch: str | None
    extensions: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class FeatureEnvironmentOverview:
    """Full picture of a feature environment — its status plus per-repo statuses."""

    status: FeatureEnvironmentStatus
    repo_statuses: list[WorktreeRepoStatus]


class SyncResult(enum.Enum):
    fast_forwarded = "fast_forwarded"
    up_to_date = "up_to_date"
    merged = "merged"
    rebased = "rebased"
    diverged = "diverged"
    no_upstream = "no_upstream"


@dataclasses.dataclass
class RepoSyncOutcome:
    """Result of syncing a single repo — whether it fast-forwarded, merged, or diverged."""

    repo_name: str
    sync_result: SyncResult
    ahead: int = 0
    behind: int = 0


@dataclasses.dataclass
class RepoDiffResult:
    """Diff output for a single repo — the raw diff text and summary statistics."""

    repo_name: str
    diff_text: str
    ahead: int
    files_changed: int
    insertions: int
    deletions: int


@dataclasses.dataclass
class WorktreeRepoStatus:
    """Summary status of one repo within a feature worktree — used in worktree-level views."""

    worktree: FeatureWorktree
    branch: str | None
    ahead: int
    behind: int
    dirty_count: int
    tracking_branch: str | None = None
    tracking_ahead: int = 0
    tracking_behind: int = 0
    tracking_ref_present: bool = False
    extensions: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class EnvSyncReport:
    """Report from syncing every repo in a feature environment — per-repo outcomes and overall success."""

    env: str
    repos: list[RepoSyncOutcome]
    success: bool


class CheckoutResult(enum.Enum):
    """Per-repo outcome of `winter ws checkout`."""

    reset = "reset"
    skip_missing_ref = "skip-missing-ref"
    refused_dirty = "refused-dirty"
    refused_divergent = "refused-divergent"


@dataclasses.dataclass
class RepoCheckoutOutcome:
    """Result of attempting to adopt a feature branch into one worktree repo."""

    repo_name: str
    result: CheckoutResult


@dataclasses.dataclass
class EnvCheckoutReport:
    """All-or-nothing report from `winter ws checkout`.

    `aborted` is True when at least one repo refused safety in Phase 1 — in that
    case no `git reset --hard` ran in any repo, and `repos` contains only the
    refusals and any skip-missing-ref findings (would-be-reset repos are not
    listed because nothing happened to them). When `aborted` is False, every
    non-pinned repo has a `reset` or `skip-missing-ref` outcome.
    """

    env: str
    feature_branch: str
    aborted: bool
    repos: list[RepoCheckoutOutcome]


@dataclasses.dataclass
class EnvDiffResult:
    """Combined diff results across every repo in a feature environment."""

    env: str
    mode: DiffMode
    repos: list[RepoDiffResult]


@dataclasses.dataclass
class RepoFetchOutcome:
    """Result of fetching one repo — name and whether the fetch succeeded."""

    repo_name: str
    success: bool
    error: str | None = None


@dataclasses.dataclass
class FetchReport:
    """Top-level fetch report — one outcome per unique project repo, plus standalone clones.

    Worktrees of a project repo share a `.git`, so a single `git fetch origin`
    updates remote refs for all of them — we fetch each project repo at most
    once even when multiple env worktrees match the user's pattern. Standalone
    clones are independent and fetched per-repo.
    """

    projects: list[RepoFetchOutcome]
    standalone: list[RepoFetchOutcome]

    @property
    def success(self) -> bool:
        if any(not r.success for r in self.projects):
            return False
        return not any(not r.success for r in self.standalone)


@dataclasses.dataclass
class EnvSkipped:
    """An env skipped by a multi-repo op (typically: not connected to a feature branch)."""

    env: str
    reason: str


@dataclasses.dataclass
class PullReport:
    """Top-level pull report — per-env sync results plus standalone outcomes."""

    envs: list[EnvSyncReport]
    standalone: list[RepoSyncOutcome]
    skipped: list[EnvSkipped] = dataclasses.field(default_factory=list)

    @property
    def success(self) -> bool:
        if any(not e.success for e in self.envs):
            return False
        if any(o.sync_result in (SyncResult.diverged, SyncResult.no_upstream) for o in self.standalone):
            return False
        return not self.skipped


class MergeResult(enum.Enum):
    """Per-repo outcome of `winter ws merge`.

    Mirrors `SyncResult` for the cases pull already covers (fast-forward,
    up-to-date, merge commit, divergence) and adds `skipped_missing_ref`
    for the merge-specific case where the source ref doesn't resolve in
    a given repo. Conflicts are reported as `diverged` — the in-progress
    merge is aborted, matching `pull --merge`'s conflict handling.
    """

    fast_forwarded = "fast_forwarded"
    up_to_date = "up_to_date"
    merged = "merged"
    diverged = "diverged"
    skipped_missing_ref = "skipped-missing-ref"


@dataclasses.dataclass
class RepoMergeOutcome:
    """Result of merging one repo — final state plus ahead/behind context."""

    repo_name: str
    result: MergeResult
    ahead: int = 0
    behind: int = 0
    error: str | None = None


_CLEAN_MERGE_RESULTS: tuple[MergeResult, ...] = (
    MergeResult.fast_forwarded,
    MergeResult.up_to_date,
    MergeResult.merged,
)


@dataclasses.dataclass
class EnvMergeReport:
    """Per-env merge outcomes (one env's selected worktrees)."""

    env: str
    repos: list[RepoMergeOutcome]

    @property
    def success(self) -> bool:
        return all(o.result in _CLEAN_MERGE_RESULTS for o in self.repos)


@dataclasses.dataclass
class MergeReport:
    """Top-level merge report — per-env outcomes plus standalone outcomes."""

    source_ref: str
    envs: list[EnvMergeReport]
    standalone: list[RepoMergeOutcome] = dataclasses.field(default_factory=list)

    @property
    def success(self) -> bool:
        if any(not env.success for env in self.envs):
            return False
        return all(o.result in _CLEAN_MERGE_RESULTS for o in self.standalone)


@dataclasses.dataclass
class RepoPushOutcome:
    """Result of pushing one repo — name, push status, commits delivered, error if any."""

    repo_name: str
    pushed: bool
    commits: int = 0
    error: str | None = None


@dataclasses.dataclass
class EnvPushReport:
    """Per-env push outcomes."""

    env: str
    repos: list[RepoPushOutcome]


@dataclasses.dataclass
class PushReport:
    """Top-level push report — per-env outcomes plus standalone outcomes."""

    envs: list[EnvPushReport]
    standalone: list[RepoPushOutcome]
    skipped: list[EnvSkipped] = dataclasses.field(default_factory=list)

    @property
    def success(self) -> bool:
        if any(not r.pushed for env in self.envs for r in env.repos):
            return False
        if any(not r.pushed for r in self.standalone):
            return False
        return not self.skipped
