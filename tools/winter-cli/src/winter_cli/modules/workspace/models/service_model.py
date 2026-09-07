from __future__ import annotations

import dataclasses
import enum
from typing import Any, Protocol, runtime_checkable

from winter_cli.modules.workspace.models.domain_model import (
    DiffMode,
    FeatureEnvironment,
    FeatureWorktree,
    ResetMode,
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
    """Git status of a single repository — branch, ahead/behind, and dirty files.

    Deliberately history-free: `RepoHistory` carries the `commit_graph` /
    `recent_commits` pair that costs a `git log --graph` subprocess. Every
    surface that only renders status (the dashboard grid, `ws status`) gathers
    this piece alone; the detail screens that also render history compose it
    with a `RepoHistory` into a `RepoStatusAndHistory`.
    """

    name: str
    path: str
    main_branch: str | None
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_files: list[str] = dataclasses.field(default_factory=list)
    staged_count: int = 0
    unstaged_count: int = 0
    untracked_count: int = 0
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
    last_commit_subject: str | None = None
    """HEAD's tip commit subject, or None when HEAD sits at parity with
    `origin/<main>` (no commits beyond it) or no main branch is configured.

    Read by a minimal `git log -1 --format=%s` probe gated on `ahead` — never
    executed when `ahead == 0`. Matches the pre-refactor
    `recent_commits[0].message` semantics without paying for the history
    walk. Populated only by `get_worktree_status_for_snapshot` (the `ws
    status` `last_commit_subject` consumer) — every other `RepoStatus`
    producer leaves this `None` so a surface that doesn't render it doesn't
    pay for the probe.
    """


@dataclasses.dataclass
class RepoHistory:
    """The expensive `git log --graph` piece of a repo's detail view — commit graph and recent commits."""

    recent_commits: list[RepoCommit] = dataclasses.field(default_factory=list)
    commit_graph: list[str] = dataclasses.field(default_factory=list)
    """`git log --graph`-style lines down to the merge-base with main.

    Each entry is one rendered graph line (graph glyphs + abbreviated hash +
    decoration + subject, the `--oneline --decorate` shape), preserving
    branch/merge topology — unlike the flat, capped `recent_commits` list,
    `commit_graph`'s detail-view-only companion within this same `RepoHistory`.
    Empty when HEAD has no commits beyond `origin/<main>` or `origin/<main>` is
    missing (fresh clone, no fetch).
    """


@dataclasses.dataclass
class RepoStatusAndHistory:
    """The composite a detail view renders — a repo's status plus its history.

    The compound name announces the extra `git log --graph` cost `RepoHistory`
    carries; gathered only by the worktree/standalone detail screens, on open
    and on their own refresh interval — never by the dashboard grid or
    `ws status`.
    """

    status: RepoStatus
    history: RepoHistory


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

    `feature_branch` is a display-only env-wide summary read from the first non-pinned repo, not a
    per-worktree truth — `ws push` / `ws pull` resolve each worktree's target from its own tracking
    config (see `WriteRepoRepository.get_worktree_push_branch`).
    `extensions` is keyed by extension prefix (e.g. `wst` for winter-service-tmux); each value
    is a short badge string an `IEnvironmentDecorator` plugin contributed for this env. Renderers
    append the values to the env header so each plugin can advertise whatever it wants.
    """

    environment: FeatureEnvironment
    feature_branch: str | None
    distinct_remote_count: int = 0
    extensions: dict[str, str] = dataclasses.field(default_factory=dict)

    def feature_branch_label(self, *, disconnected: str = "—") -> str:
        """The env's branch as shown in the dashboard, with a `+N` multi-remote suffix.

        `feature_branch` is the primary — the first *connected* non-pinned repo's
        branch. When the env's worktrees span more than one distinct remote
        branch, the label gains `+N`, where N is the number of *additional*
        distinct remotes (`distinct_remote_count - 1`) — so a 5-distinct-remote
        env reads `feature-x+4`. `feature_branch` is `None` only when *no*
        non-pinned worktree is connected, in which case the label is just the
        `disconnected` placeholder.
        """
        if self.feature_branch is None:
            return disconnected
        if self.distinct_remote_count > 1:
            return f"{self.feature_branch}+{self.distinct_remote_count - 1}"
        return self.feature_branch


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
    held_pin = "held_pin"
    re_pinned = "re_pinned"
    pin_error = "pin_error"
    """A re-pin or branch-pin advance failed (dirty-tree refusal, unresolvable ref,
    or checkout error) — distinct from a genuine upstream divergence (``diverged``).

    A true branch-pin ff-refusal-on-divergence uses ``diverged`` because HEAD has
    genuinely diverged from ``origin/<ref>`` — that is the correct semantic. All
    other pin operation failures (dirty guard, resolve error, stash/pop failure) use
    ``pin_error`` so callers can distinguish "git divergence" from "pin operation
    could not run at all".
    """


@dataclasses.dataclass
class RepoSyncOutcome:
    """Result of syncing a single repo — whether it fast-forwarded, merged, or diverged.

    `commits` is the number of upstream commits integrated on a successful
    fast-forward / merge / rebase (0 when already up to date). `ahead` /
    `behind` carry the divergence span and are populated only for the
    `diverged` outcome. `pin_ref` carries pin metadata for `held_pin`
    (the held ref string, e.g. ``v1.4.2``) and `re_pinned` (the new
    short SHA the lock was advanced to). `commits` is the shared field
    name used across the fetch / pull / push outcome models.
    """

    repo_name: str
    sync_result: SyncResult
    commits: int = 0
    ahead: int = 0
    behind: int = 0
    pin_ref: str = ""


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
    """Per-env integration result — per-repo outcomes and overall success.

    Nested inside `PullReport` (one per matched env); the name predates the
    `fetch`/`pull`/`merge` split and is retained because `pull_all` builds it.
    """

    env: str
    repos: list[RepoSyncOutcome]
    success: bool


class CheckoutResult(enum.Enum):
    """Per-repo outcome of `winter ws checkout`.

    `reset_feature` / `reset_main` are the two success shapes: the worktree was
    connected to `origin/<feature-branch>` and hard-reset either to that ref
    (when it exists locally) or to `origin/<main-branch>` (when the feature ref
    is absent — a brand-new branch started from main, opted into with `--new`).
    The rest are Phase 1 refusals. `refused_dirty` / `refused_abandonment` are
    the safety gate, bypassable with `--force`. The two ref-resolution refusals
    are *not* bypassed by `--force`: `refused_unknown_branch` means the feature
    ref resolved in no repo and `--new` wasn't given (more likely a typo or a
    missing `winter ws fetch` than a new branch); `refused_missing_ref` means
    neither the feature ref nor `origin/<main-branch>` resolves in that repo,
    so there is nothing to reset to.
    """

    reset_feature = "reset-feature"
    reset_main = "reset-main"
    refused_dirty = "refused-dirty"
    refused_abandonment = "refused-abandonment"
    refused_unknown_branch = "refused-unknown-branch"
    refused_missing_ref = "refused-missing-ref"


@dataclasses.dataclass
class RepoCheckoutOutcome:
    """Result of attempting to adopt a feature branch into one worktree repo."""

    repo_name: str
    result: CheckoutResult


@dataclasses.dataclass
class EnvCheckoutReport:
    """All-or-nothing report from `winter ws checkout`.

    `aborted` is True when at least one repo refused in Phase 1 (safety gate or
    ref resolution) — in that case no connect and no `git reset --hard` ran in
    any repo, and `repos` contains only the refusals (would-be-reset repos are
    not listed because nothing happened to them). When `aborted` is False,
    every non-pinned repo was connected and has a `reset_feature` or
    `reset_main` outcome.
    """

    env: str
    feature_branch: str
    aborted: bool
    repos: list[RepoCheckoutOutcome]


class ResetResult(enum.Enum):
    """Per-repo outcome of `winter ws reset`.

    `reset` is the sole success shape, for every mode — the report's `mode`
    field (not a per-repo enum member) records which of soft/mixed/hard
    actually ran. `refused_dirty` / `refused_abandonment` are the `--hard`-only
    safety gate, bypassable with `--force`; `--soft`/`--mixed` never produce
    them. `refused_missing_ref` fires when REF doesn't resolve locally in a
    matched repo — not bypassable by `--force`, since there is nothing to
    reset to. `refused_ambiguous_sha` fires when REF is a bare commit SHA and
    more than one worktree matched — a commit exists in exactly one repo's
    history and has no meaning in another's, so the whole run refuses rather
    than guessing which repo it was meant for.
    """

    reset = "reset"
    refused_dirty = "refused-dirty"
    refused_abandonment = "refused-abandonment"
    refused_missing_ref = "refused-missing-ref"
    refused_ambiguous_sha = "refused-ambiguous-sha"


@dataclasses.dataclass
class RepoResetOutcome:
    """Result of attempting to reset one matched worktree — env-qualified since
    `winter ws reset` PATTERNS may span more than one environment."""

    env: str
    repo_name: str
    result: ResetResult
    ref: str = ""
    """The per-repo resolved REF (after `{main}`-token expansion) the worktree
    was — or, under `--dry-run`, would be — reset to. Empty for refusals that
    never got far enough to resolve one (e.g. `refused_ambiguous_sha`)."""


@dataclasses.dataclass
class RepoCleanOutcome:
    """Untracked paths removed from — or, under `--dry-run`, that would be
    removed from — one matched worktree. Env-qualified since `winter ws clean`
    PATTERNS may span more than one environment."""

    env: str
    repo_name: str
    paths: list[str] = dataclasses.field(default_factory=list)
    """Worktree-relative paths of the untracked files, in git's own order.
    Ignored files are never listed, because they are never removed. A worktree
    with nothing untracked carries an empty list and is still reported, so the
    run shows every worktree it considered rather than only the ones it hit."""

    @property
    def count(self) -> int:
        return len(self.paths)


@dataclasses.dataclass
class CleanFailure:
    """The worktree a `winter ws clean` run stopped on, and why.

    Present only when a git operation raised. The paths it had already removed
    are still carried by that worktree's `RepoCleanOutcome`, because `git
    clean -fd` deletes what it can before failing and those deletions are
    unrecoverable.
    """

    env: str
    repo_name: str
    message: str


@dataclasses.dataclass
class CleanReport:
    """Report from `winter ws clean`.

    Unlike `ResetReport` there is no `aborted` field and no per-repo refusal
    enum: cleaning has no precondition to violate — no ref to resolve, no
    history to abandon — so the confirmation prompt, not a Phase 1 gate, is
    what stands between the caller and the deletion. `dry_run` distinguishes a
    preview (paths enumerated, nothing removed) from a real run.

    `failure` records a run that stopped partway. The run is **not**
    all-or-nothing — it cannot be, since a deleted file has nothing to roll
    back to — so `repos` still lists every worktree processed before the
    failure plus whatever the failing one had already lost. That partial
    record is the only trace those files ever existed.

    Ignored files are outside this report entirely, in both modes: `winter ws
    clean` never removes them, so a worktree whose only untracked content is
    ignored reports zero paths.
    """

    dry_run: bool
    repos: list[RepoCleanOutcome]
    failure: CleanFailure | None = None

    @property
    def total(self) -> int:
        return sum(o.count for o in self.repos)


@dataclasses.dataclass
class ResetReport:
    """All-or-nothing report from `winter ws reset`.

    `aborted` is True when at least one matched repo refused in Phase 1 (the
    ambiguous-SHA guard, a missing REF, or — `--hard` only, unless `--force`
    — dirty / abandonment) — in that case no `git reset` ran in any repo, and
    `repos` contains only the refusals. When `aborted` is False, every matched
    repo carries a `reset` outcome — for real when `dry_run` is False, or as a
    plan preview (no git op executed) when `dry_run` is True. Upstream
    tracking is never touched, by any mode, in either phase.
    """

    ref: str
    mode: ResetMode
    dry_run: bool
    aborted: bool
    repos: list[RepoResetOutcome]


@dataclasses.dataclass
class EnvDiffResult:
    """Combined diff results across every repo in a feature environment."""

    env: str
    mode: DiffMode
    repos: list[RepoDiffResult]


@dataclasses.dataclass
class RepoFetchOutcome:
    """Result of fetching one repo — name, success, and how far local main advanced.

    `commits` is the number of commits the source checkout's local main was
    fast-forwarded by this fetch (0 when already up to date). Standalone
    clones are only fetched, not fast-forwarded, so their `commits` stays 0.
    Shares the `commits` field name with `RepoSyncOutcome` / `RepoPushOutcome`.
    """

    repo_name: str
    success: bool
    commits: int = 0
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
        if any(
            o.sync_result in (SyncResult.diverged, SyncResult.no_upstream, SyncResult.pin_error)
            for o in self.standalone
        ):
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
class LocalFastForward:
    """Result of the post-push sync of the workspace's local copy of a shared branch.

    When a worktree pushes to a long-lived branch (e.g. `main`), winter tries to
    fast-forward the source checkout's local copy of that branch so it stays in
    sync. `advanced` records whether the ff happened; `skipped_reason` explains a
    no-op that was still worth surfacing (local branch not in sync, dirty, or the
    ff itself failed).
    """

    branch: str
    advanced: bool
    commits: int = 0
    skipped_reason: str | None = None


@dataclasses.dataclass
class RepoPushOutcome:
    """Result of pushing one repo — name, push status, commits delivered, error if any.

    `local_ff` is set only when the push targeted a repo's main branch: it records
    whether the workspace's local main was fast-forwarded to the pushed tip.
    """

    repo_name: str
    pushed: bool
    commits: int = 0
    error: str | None = None
    local_ff: LocalFastForward | None = None


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


@dataclasses.dataclass
class RebaseConflict:
    """Detail of a `rebase_onto` conflict — the repo is left mid-rebase, exactly
    where git stopped, rather than rolled back.

    `replayed_commit` is the full hash of the commit git was replaying when the
    conflict stopped it (read from the rebase state git itself keeps on disk).
    `conflicted_paths` are the worktree-relative paths git reports unmerged.
    """

    replayed_commit: str
    conflicted_paths: list[str]


@dataclasses.dataclass
class RebaseOntoResult:
    """Outcome of one `rebase_onto` call — clean, or left mid-rebase.

    `conflict` is `None` on a clean rebase, including the zero-commit
    fast-forward case where the branch carries nothing past its old base.
    When the replay stops, `conflict` carries the detail and the worktree is
    left mid-rebase for the caller to resolve or continue — `rebase_onto`
    never aborts.
    """

    conflict: RebaseConflict | None = None

    @property
    def success(self) -> bool:
        return self.conflict is None


class BoundarySource(enum.Enum):
    """Where a `RestackLink`'s frozen boundary sha came from for one repo.

    `fork_point` is the derived default — `git merge-base --fork-point
    <predecessor> <env>`, read from the predecessor's reflog so a local
    rewrite of the predecessor doesn't pull the env's own commits back into
    the replay. `cut` is the operator's assertion for the bottom-most link,
    supplied in place of a fork point when ancestry to the predecessor was
    severed (a squash-landed predecessor, for instance) — its tip is read
    directly, never derived.
    """

    fork_point = "fork-point"
    cut = "cut"


class RestackRefusal(enum.Enum):
    """Why `EnvRestackPlanService` refused a run — all-or-nothing, like every
    other pre-flight guard in this codebase: any one firing means no link
    executes anywhere.

    `refused_missing_ref` covers two distinct triggers sharing one code: a
    chain element or the base resolving in no repo at all (nothing for that
    link to act on anywhere), and a `--cut` ref that fails to resolve in a
    repo where the bottom link otherwise participates (there is nothing to
    read its tip from). `refused_dirty` / `refused_rebase_in_progress` /
    `refused_detached_head` are the worktree-safety guards, each scoped to
    participating repos only — a repo that never entered the run because its
    env branch doesn't resolve there is `skipped`, not refused, and never
    reaches these checks. `refused_inverted_order` covers a repeated element
    (the same ref named twice among the chain, the base, and `--cut`) and the
    chain-level ancestry inversion described on `RestackPlan`.
    `refused_unknown_boundary` is a fork-point probe that came back empty —
    no reflog entry ties the env to its predecessor — and `--cut` reaches
    only the bottom link, so an upper link with no fork point has no
    in-command recourse. `refused_cut_not_ancestor` is `--cut`'s third
    per-repo class: neither the cut's tip nor the base's tip is an ancestor
    of the bottom env's branch, so there is nothing for the cut to assert.
    """

    refused_missing_ref = "refused-missing-ref"
    refused_dirty = "refused-dirty"
    refused_rebase_in_progress = "refused-rebase-in-progress"
    refused_detached_head = "refused-detached-head"
    refused_inverted_order = "refused-inverted-order"
    refused_unknown_boundary = "refused-unknown-boundary"
    refused_cut_not_ancestor = "refused-cut-not-ancestor"


@dataclasses.dataclass
class RestackPlanRefusal:
    """One reason `EnvRestackPlanService` refused the run, naming the link it
    came from and the repos that triggered it.

    `repos` is empty for a refusal that names a chain shape rather than a
    repo's git state — a repeated element, for instance, is wrong before any
    repo is even opened. Every other code names the specific repos that
    triggered it, not every repo the link would have touched.

    `element` disambiguates `refused_missing_ref`'s three triggers, which
    otherwise render identically — the same `env`/`predecessor` pair, and
    empty or identical `repos` — leaving an operator unable to tell a
    mistyped chain element from a mistyped `BASE` from an unresolvable
    `--cut`: the literal env name when the chain element itself resolves
    nowhere, the literal base value when the trailing base resolves
    nowhere, or the literal `--cut` when the cut ref is what failed to
    resolve. Also populated on `refused_inverted_order` specifically when a
    `--cut` value collides with a chain member or `BASE`: `env`/`predecessor`
    there read identically to an ordinary bottom link (`(chain[-1], base)`),
    naming neither `--cut` nor the ref that collided, so `element` carries
    that literal cut ref. `None` for every other refusal code — including
    `refused_inverted_order`'s other trigger, a value repeated within the
    chain/base themselves, where the colliding value is already `env` — where
    `env` / `predecessor` (and `repos`, where populated) already say
    everything there is to say.
    """

    result: RestackRefusal
    env: str
    predecessor: str
    repos: list[str] = dataclasses.field(default_factory=list)
    element: str | None = None


@dataclasses.dataclass
class RestackLinkRepo:
    """One repo's frozen participation in a `RestackLink`.

    `boundary` and `source` are populated only when the repo participates —
    both the env branch and the link's predecessor resolved there, in that
    repo's own worktree. A non-participating repo carries `boundary=None`,
    `source=None`: `skipped`, settled by ref resolution alone, before any
    worktree status is ever read.
    """

    repo_name: str
    boundary: str | None = None
    source: BoundarySource | None = None

    @property
    def participates(self) -> bool:
        return self.boundary is not None


@dataclasses.dataclass
class RestackLink:
    """One rebase-onto-predecessor step of a restack chain — the env, its
    predecessor, and every non-pinned project repo's frozen participation.

    `env` is the branch being replayed; `predecessor` is what it replays
    onto — another chain element for every link but the bottom-most, whose
    predecessor is the trailing base. Neither the `--onto` execution target
    nor an `up-to-date` verdict lives here: the predecessor moves before an
    upper link runs, so both are resolved fresh at execution time against
    whatever the predecessor's tip has become by then.
    """

    env: str
    predecessor: str
    repos: list[RestackLinkRepo] = dataclasses.field(default_factory=list)

    @property
    def participating_repos(self) -> list[str]:
        return [r.repo_name for r in self.repos if r.participates]

    @property
    def skipped_repos(self) -> list[str]:
        return [r.repo_name for r in self.repos if not r.participates]


@dataclasses.dataclass
class RestackPlan:
    """Result of `EnvRestackPlanService.plan` — a validated, per-link,
    per-repo plan, or the refusals that stopped it before any link was built.

    `links` is ordered for base-ward execution — the reverse of the argument
    order the chain was named in — so an executor can run it front to back
    and always replay a link onto its predecessor's already-restacked tip.
    `links` is empty whenever `refusals` is non-empty: a refusal is
    all-or-nothing, so nothing about the run is safe to report as a plan.

    A chain typed backwards — the operator naming an env above the one it
    actually branched from — is not among the refusals `plan` can produce.
    When the element named on top has not advanced since the other branched
    from it, every repo carries no commits past its frozen boundary, so the
    inversion check's exemption applies everywhere and it cannot fire; the
    link executes as a fast-forward and the stack collapses into one branch.
    When it has advanced, the exemption lapses but so does the ancestry test
    the refusal also requires, and the link replays that element's
    post-branch commits onto the other env instead. Both are a declared
    limit of the chain-level check, not a bug in it: the two chains are
    topologically identical to a legitimate one where a new env is stacked
    on a predecessor that has since advanced, so no git-level test tells them
    apart, and the operator's naming is taken as authoritative rather than
    second-guessed.
    """

    links: list[RestackLink] = dataclasses.field(default_factory=list)
    refusals: list[RestackPlanRefusal] = dataclasses.field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals)


class RestackResult(enum.Enum):
    """Per-repo, per-link outcome of `EnvRestackService.execute` — Decision
    5's three-way execution classification.

    `up_to_date` fires when the predecessor's *execution-time* tip is
    already an ancestor of the env branch — how a link that already
    completed reads on a re-run, and how a `cut already done` repo reads
    once its excluded range has actually been dropped. `rebased` fires when
    the env's own commits replayed onto the predecessor's new tip,
    including the zero-commit case where an env with nothing past its
    boundary simply fast-forwards. `conflict` never appears on a
    `RepoRestackOutcome` in `RestackReport.completed` — a conflicting repo
    stops the run and is reported through `RestackReport.conflict` instead,
    which carries the detail this enum alone can't.
    """

    up_to_date = "up-to-date"
    rebased = "rebased"
    conflict = "conflict"


class RestackOutcome(enum.Enum):
    """The full `outcome` domain the `restack` render vocabulary uses —
    `RestackResult`'s three execution classes plus `skipped`, the fourth
    value a non-participating repo renders as.

    Kept separate from `RestackResult` rather than adding `skipped` there:
    that enum's own doc pins it to Decision 5's three-way *execution*
    classification, computed by `EnvRestackService`, which never itself
    decides "skipped" (that's `RestackLinkRepo.participates`, settled at
    plan time). This enum exists solely so the handler's table/JSON render
    routes every one of its four wire values through the type system —
    `RestackResult.value` and this enum's values are kept in lockstep by
    construction below — instead of duplicating `"conflict"` / `"skipped"`
    as bare string literals a renamed member could silently desync from.
    """

    up_to_date = RestackResult.up_to_date.value
    rebased = RestackResult.rebased.value
    conflict = RestackResult.conflict.value
    skipped = "skipped"


@dataclasses.dataclass
class RepoRestackOutcome:
    """One participating repo's outcome from executing one `RestackLink`.

    `env` identifies which link this outcome belongs to — a chain names
    each env exactly once, so it disambiguates without also carrying the
    predecessor.
    """

    env: str
    repo_name: str
    result: RestackResult


@dataclasses.dataclass
class RestackConflict:
    """Where an `EnvRestackService.execute` run stopped — the repo
    `rebase_onto` left mid-rebase, exactly where git stopped rather than
    rolled back.

    Report-shaped wrapper over the git-seam `RebaseConflict`: adds the
    env/repo identity a report needs to name where to resolve, so a caller
    doesn't need to reach into a raw `RebaseOntoResult` itself. `env` is
    replayed *in*; `predecessor` is what it's being replayed *onto* — the
    two are not interchangeable, and a report that only named `env` would
    leave an operator unable to tell which side of the link stopped.
    """

    env: str
    predecessor: str
    repo_name: str
    replayed_commit: str
    conflicted_paths: list[str]


@dataclasses.dataclass
class RestackFailure:
    """The link/repo an `EnvRestackService.execute` run stopped on because
    the repo seam raised — a non-conflict git failure, distinct from
    `RestackConflict`'s mid-rebase stop.

    Mirrors `CleanFailure` (`env_clean_service.py`'s idiom for exactly this
    shape): present only when a git operation raised something other than a
    conflict-stop — `rebase_onto` itself raises `RepoError` whenever
    `_read_rebase_conflict` finds no on-disk rebase state to report, e.g. a
    plain non-conflict failure like refused-dirty's untracked-file gap
    (`git rebase` aborting because an untracked path in the way would be
    overwritten). Every repo `completed` before the failure — including in
    earlier links this same run already rewrote — is preserved on the
    report rather than discarded with the stack frame.
    """

    env: str
    repo_name: str
    message: str


@dataclasses.dataclass
class RestackReport:
    """Result of `EnvRestackService.execute` — every repo outcome the run
    completed, in execution order, plus — when a rebase conflicted or the
    repo seam raised — the detail and everything the run stopped short of.

    `completed` lists every participating repo that finished cleanly
    (`up_to_date` or `rebased`), across every link the run got through, in
    the base-ward execution order `plan.links` was already ordered in.
    `conflict` is `None` on a run that made it through the whole plan; when
    it isn't, the run stopped there — no repo past it, in that link or any
    upper one, was ever touched. `failure` is the same shape for the other
    way a repo can stop the run: a non-conflict git failure (`RepoError`)
    raised out of the repo seam, caught here rather than left to unwind past
    every already-completed outcome. At most one of `conflict` / `failure`
    is ever set — the run stops at the first repo that hits either.
    `remaining` is the tail of `plan.links` the run never finished: the
    stopping link itself (not every one of its repos completed) through the
    last one, wrapped in a `RestackPlan` so a caller can render it with the
    same shape a fresh plan has. Empty (`links=[]`, the default) on a run
    that completed the whole plan.

    A conflict-stopped run is resumed by re-running the same command — same
    chain, same `--cut` — rather than by feeding `remaining` back into
    `execute` directly: both `EnvRestackPlanService.plan` and
    `EnvRestackService.execute` re-derive every boundary and every repo's
    classification fresh each call, so a from-scratch re-run reaches the
    same state a `remaining`-only continuation would. A failure-stopped run
    is resumed the same way once whatever the seam raised on is fixed
    (e.g. the untracked file removed or stashed) — nothing about the repo
    is left mid-rebase, since `rebase_onto` only raises when it *isn't*
    a conflict-stop.
    """

    completed: list[RepoRestackOutcome]
    conflict: RestackConflict | None = None
    failure: RestackFailure | None = None
    remaining: RestackPlan = dataclasses.field(default_factory=RestackPlan)

    @property
    def success(self) -> bool:
        return self.conflict is None and self.failure is None
