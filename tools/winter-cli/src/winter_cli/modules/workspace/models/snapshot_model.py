from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class StandalonePinSnapshot:
    """Read-only pin/lock state for one standalone repo that has a configured ``ref``.

    Fields
    ------
    name:
        Matches ``[[standalone_repository]].name`` in the config.
    ref:
        The ``ref`` string from the config (branch name, tag, or commit SHA).
    kind:
        How the ref is classified: ``"branch"``, ``"tag"``, or ``"commit"``.
        ``None`` when the lock entry is absent (never been pinned/updated).
    locked_commit:
        The full 40-character SHA recorded in ``.winter/config.lock`` at
        the last ``ws update`` or ``ws init``. ``None`` when no lock entry
        exists yet.
    config_ref_drift:
        True when the lock file records a *different* ref than what the config
        currently declares (lock is stale — a ``ws update`` is needed).
        False when they match or when no lock entry exists.
    head_drift:
        True when the repo's HEAD commit does not match ``locked_commit``
        (the checkout has drifted from the recorded pin). False when they
        match or when no lock entry exists.
    head_commit:
        The current HEAD commit of the standalone repo (full 40-char SHA), or
        ``None`` when the repo is absent on disk or the probe fails.
    """

    name: str
    ref: str
    kind: str | None
    locked_commit: str | None
    config_ref_drift: bool
    head_drift: bool
    head_commit: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class OrphanSnapshot:
    """An orphaned filesystem entry — a directory or file with no declared owner.

    `kind` is a short label such as ``"worktree_dir"``, ``"env_dir"``, or
    ``"git_worktree"`` so renderers can group or filter orphans by type.
    `safe_to_remove` is True when the collector has determined the entry can
    be deleted without data loss (e.g. no uncommitted changes, no live git
    worktree registration). `notes` is a free-form human-readable explanation.
    """

    kind: str
    path: str
    safe_to_remove: bool
    notes: str


@dataclasses.dataclass(frozen=True, slots=True)
class WorkspaceLevelSnapshot:
    """Workspace-wide metadata — extensions, orphans, drift findings, and pin state.

    `extensions` lists the names of installed standalone repos (extensions),
    e.g. ``["winter-github", "winter-harness"]``. `drift_missing` names repo
    directories declared in config but absent on disk; `drift_undeclared` names
    directories present under the projects root but not declared in config.
    `standalone_pins` carries per-standalone pin/lock state for every declared
    standalone that has a ``ref`` configured — empty list when no pins exist.
    """

    root_path: str
    extensions: list[str]
    orphans: list[OrphanSnapshot]
    drift_missing: list[str]
    drift_undeclared: list[str]
    standalone_pins: list[StandalonePinSnapshot] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    """Per-repo snapshot inside a feature environment worktree.

    `upstream` is the configured remote-tracking ref (e.g.
    ``"origin/feature/my-branch"``), or None when no upstream is configured.
    `ahead`/`behind` are relative to ``origin/<main-branch>``; the
    `tracking_*` fields are relative to the configured upstream ref.
    `staged`, `unstaged`, and `untracked` are file counts; `dirty` is the
    deduplicated union of staged, unstaged, and untracked. `last_commit_subject`
    is the first line of the most recent commit message, or None when the
    branch has no commits beyond origin/<main>.
    """

    repo: str
    branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    tracking_ahead: int
    tracking_behind: int
    tracking_ref_present: bool
    staged: int
    unstaged: int
    untracked: int
    dirty: int
    last_commit_subject: str | None
    pinned: bool = False
    main_branch: str | None = None
    # NOTE: WorktreeRepoStatus.extensions (per-worktree plugin badges) is intentionally
    # NOT serialized into WorktreeSnapshot. The TUI renders per-cell badges directly from
    # WorktreeRepoStatus; the JSON contract omits them because no tested serialization
    # path exists yet. Add an `extensions: dict[str, str]` field here when a worktree-level
    # decorator is available to drive it end-to-end.


@dataclasses.dataclass(frozen=True, slots=True)
class EnvSnapshot:
    """Full snapshot of one feature environment.

    `feature_branch` is a display-only env-wide summary read from the first
    non-pinned repo (e.g. ``"feature/my-branch"``), or None when the env is not
    yet connected — not a per-worktree truth. `ws push` / `ws pull` resolve each
    worktree's target independently from its own tracking config, so a worktree
    re-pointed to a different branch is not reflected here.
    `port_base` is the env's assigned port base derived from its index.
    `extensions` carries per-plugin badge strings contributed by environment
    decorator plugins (e.g. service status badges from winter-service-tmux). The
    dict is keyed by plugin prefix (e.g. ``"wst"``); values are short display
    strings (e.g. ``"● 3/3"``). Empty when no decorators ran or all wrote "".
    """

    name: str
    index: int
    port_base: int
    feature_branch: str | None
    worktrees: list[WorktreeSnapshot]
    extensions: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class SourceCheckoutSnapshot:
    """Snapshot of a source checkout (project main clone or standalone repo).

    `behind_origin` and `ahead_origin` are relative to ``origin/<main-branch>``.
    `dirty` is the count of changed files (staged + unstaged + untracked).
    `drift` lists any drift findings specific to this checkout (e.g. missing
    declared sub-paths).
    """

    repo: str
    branch: str | None
    behind_origin: int
    ahead_origin: int
    dirty: int
    drift: list[str]


@dataclasses.dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Configured and resolved dashboard grid layout for the current workspace shape.

    `configured_layout` is the `[tui.dashboard] layout` config value verbatim
    (e.g. ``"auto"``, ``"list"``). `resolved_layout` is the concrete layout that
    value resolves to for the current workspace shape — identical to
    `configured_layout` unless it is ``"auto"``, in which case the shared
    `DashboardLayout.resolve` policy (the same one the dashboard TUI grid uses,
    fed counts derived the same way) picks the concrete layout from the per-env
    worktree count and the env count. Both are the enum's string value, never the
    enum object. Exposed on `winter ws status --json` so agents can confirm
    `auto` resolution without driving the interactive Textual TUI. The resolution
    reflects the whole-workspace shape and is unaffected by `ws status` patterns.
    """

    configured_layout: str
    resolved_layout: str


@dataclasses.dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Top-level machine-readable workspace state snapshot.

    `schema_version` is 1 for this release. Consumers should reject or warn
    on unexpected versions. All sub-snapshots are pure data with no behavior —
    renderers select the slice they need.
    """

    schema_version: int
    workspace: WorkspaceLevelSnapshot
    environments: list[EnvSnapshot]
    source_checkouts: list[SourceCheckoutSnapshot]
    dashboard: DashboardSnapshot
