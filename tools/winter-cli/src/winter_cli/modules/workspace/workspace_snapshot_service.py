from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

import click

from winter_cli.config.models import DashboardLayout
from winter_cli.modules.workspace.config_lock_repository import IConfigLockRepository
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.models import (
    DashboardSnapshot,
    EnvSnapshot,
    FeatureEnvironment,
    FeatureEnvironmentOverview,
    FeatureWorktree,
    OrphanSnapshot,
    ProjectRepository,
    RepoError,
    SourceCheckoutSnapshot,
    StandalonePinSnapshot,
    StandaloneRepository,
    StandaloneRepoStatus,
    Workspace,
    WorkspaceLevelSnapshot,
    WorkspaceSnapshot,
    WorktreeRepoStatus,
    WorktreeSnapshot,
)
from winter_cli.modules.workspace.pattern_match import matches_any_pattern
from winter_cli.modules.workspace.prune_service import PruneService
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.plugins.types import IEnvironmentDecorator, IWorktreeRepoDecorator

logger = logging.getLogger(__name__)


class WorkspaceSnapshotService:
    """Composes existing lower-layer services into a `WorkspaceSnapshot`.

    This service does NO git-probing of its own; it orchestrates what already
    exists (`EnvStatusService`, `DriftWarningService`, `PruneService`, the repo
    repositories, and the factory). Both the dashboard TUI (Phase 5) and the
    `ws status` command (Phase 3) consume this service so the two surfaces cannot
    disagree on what they read.
    """

    def __init__(
        self,
        workspace: Workspace,
        env_status_svc: EnvStatusService,
        workspace_repo: IReadWorkspaceRepository,
        repo_repo: IWriteRepoRepository,
        repo_factory: RepositoryFactory,
        drift_warning_svc: DriftWarningService,
        prune_svc: PruneService,
        config_lock_repo: IConfigLockRepository,
        git_repo: IGitRepository,
        dashboard_layout: DashboardLayout = DashboardLayout.auto,
    ) -> None:
        self._workspace = workspace
        self._env_status_svc = env_status_svc
        self._workspace_repo = workspace_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._drift_warning_svc = drift_warning_svc
        self._prune_svc = prune_svc
        self._config_lock_repo = config_lock_repo
        self._git_repo = git_repo
        self._dashboard_layout = dashboard_layout

    def collect(
        self,
        *,
        patterns: list[str] | None = None,
        on_repo_error: Callable[[FeatureWorktree, RepoError], None] | None = None,
        env_decorators: list[IEnvironmentDecorator] | None = None,
        worktree_repo_decorators: list[IWorktreeRepoDecorator] | None = None,
    ) -> WorkspaceSnapshot:
        """Collect a complete workspace state snapshot.

        Parameters
        ----------
        patterns:
            When non-empty, filter the environments and worktrees to those
            matching at least one pattern (segment-aware glob over
            ``<env>/<repo>``).  Bare env names expand to ``<env>/*``.
            Envs that end up with zero matching worktrees are dropped from the
            snapshot.  Source-checkout and workspace-level sections are always
            included as context.  When the list is empty or ``None``, all
            environments and worktrees are returned.  Raises
            ``click.ClickException`` when no worktree matches any pattern.
        on_repo_error:
            Mirrors `EnvStatusService`'s tolerate-vs-propagate contract. The
            dashboard (Phase 5) passes a skip+log callback so one broken repo
            doesn't poison the whole refresh. CLI (Phase 3) passes ``None`` so
            the first error propagates and the command exits non-zero.
        env_decorators:
            Optional list of `IEnvironmentDecorator` plugins that may write into
            `FeatureEnvironmentStatus.extensions` (dashboard-only; CLI passes
            ``None``).
        worktree_repo_decorators:
            Optional list of `IWorktreeRepoDecorator` plugins (dashboard-only).
        """
        effective_patterns: list[str] = patterns or []
        project_repos = self._repo_factory.get_project_repos()

        # ── environments ──────────────────────────────────────────────────
        environments = self._workspace_repo.get_environments(self._workspace, project_repos)

        env_snapshots: list[EnvSnapshot] = []
        # Full (pre-pattern-filter) env overviews, in env order — the same
        # structure the dashboard TUI grid consumes. The dashboard layout block
        # is resolved from these so the CLI and the grid feed `resolve` inputs
        # derived identically (per-env worktree count, env count).
        overviews: list[FeatureEnvironmentOverview] = []
        total_matched_worktrees = 0
        for env in environments:
            overview = self._collect_env_overview(
                env,
                project_repos,
                on_repo_error=on_repo_error,
                env_decorators=env_decorators,
                worktree_repo_decorators=worktree_repo_decorators,
            )
            if overview is None:
                # Env-level probe failed and was tolerated (dashboard-style
                # callback). The CLI passes on_repo_error=None, so this never
                # happens on the `ws status` path — the error propagates instead.
                continue
            overviews.append(overview)
            env_status = overview.status

            worktree_snapshots: list[WorktreeSnapshot] = []
            for wt_status in overview.repo_statuses:
                repo_name = wt_status.worktree.repository.name
                # Apply pattern filter: skip worktrees that don't match any pattern.
                if effective_patterns and not matches_any_pattern(env.name, repo_name, effective_patterns):
                    continue

                # get_worktree_repo_statuses returns coarse RepoStatus objects
                # (no staged/unstaged/untracked breakdown). Re-probe via
                # get_worktree_status to recover the fine-grained counts that
                # WorktreeSnapshot requires. The dashboard path skips this
                # second probe because no dashboard field needs the breakdown.
                try:
                    rs = self._repo_repo.get_worktree_status(wt_status.worktree)
                except RepoError as exc:
                    if on_repo_error is None:
                        raise
                    on_repo_error(wt_status.worktree, exc)
                    continue

                last_subject: str | None = None
                if rs.recent_commits:
                    last_subject = rs.recent_commits[0].message

                worktree_snapshots.append(
                    WorktreeSnapshot(
                        repo=repo_name,
                        branch=rs.branch,
                        upstream=rs.tracking_branch,
                        ahead=rs.ahead,
                        behind=rs.behind,
                        tracking_ahead=rs.tracking_ahead,
                        tracking_behind=rs.tracking_behind,
                        tracking_ref_present=rs.tracking_ref_present,
                        staged=rs.staged_count,
                        unstaged=rs.unstaged_count,
                        untracked=rs.untracked_count,
                        dirty=len(rs.dirty_files),
                        last_commit_subject=last_subject,
                        pinned=wt_status.worktree.repository.pinned,
                        main_branch=wt_status.worktree.repository.main_branch,
                    )
                )

            # When patterns are active, drop envs that ended up with no matching worktrees.
            if effective_patterns and not worktree_snapshots:
                continue

            total_matched_worktrees += len(worktree_snapshots)
            env_snapshots.append(
                EnvSnapshot(
                    name=env.name,
                    index=env.index,
                    port_base=self._workspace.port_base_for(env.index),
                    feature_branch=env_status.feature_branch,
                    worktrees=worktree_snapshots,
                    extensions=dict(env_status.extensions),
                )
            )

        # Zero-match guard: if patterns were given but nothing matched, raise.
        if effective_patterns and total_matched_worktrees == 0:
            raise click.ClickException(f"No worktrees match: {', '.join(effective_patterns)}")

        # ── source checkouts (project main clones) ────────────────────────
        drift_report = self._drift_warning_svc.detect()
        missing_names = {r.name for r in drift_report.missing}

        source_checkout_snapshots: list[SourceCheckoutSnapshot] = []

        main_statuses = self._collect_main_branch_statuses(project_repos, tolerate=on_repo_error is not None)

        for repo in project_repos:
            drift_notes: list[str] = []
            if repo.name in missing_names:
                drift_notes.append("missing from projects/")

            wt_status = main_statuses.get(repo.name)
            if wt_status is not None:
                source_checkout_snapshots.append(
                    SourceCheckoutSnapshot(
                        repo=repo.name,
                        branch=wt_status.branch,
                        behind_origin=wt_status.behind,
                        ahead_origin=wt_status.ahead,
                        dirty=wt_status.dirty_count,
                        drift=drift_notes,
                    )
                )
            elif drift_notes:
                # Repo has drift notes but no git status (missing on disk) —
                # still include it so callers see the drift finding.
                source_checkout_snapshots.append(
                    SourceCheckoutSnapshot(
                        repo=repo.name,
                        branch=None,
                        behind_origin=0,
                        ahead_origin=0,
                        dirty=0,
                        drift=drift_notes,
                    )
                )

        # Add undeclared dirs as drift entries (no corresponding ProjectRepository)
        for undeclared_name in drift_report.undeclared:
            source_checkout_snapshots.append(
                SourceCheckoutSnapshot(
                    repo=undeclared_name,
                    branch=None,
                    behind_origin=0,
                    ahead_origin=0,
                    dirty=0,
                    drift=["undeclared in config"],
                )
            )

        # ── workspace-level ───────────────────────────────────────────────
        orphan_raw = self._prune_svc.find_orphans()
        orphan_snapshots = [
            OrphanSnapshot(
                kind=o.kind,
                path=str(o.path),
                safe_to_remove=o.safe_to_remove,
                notes=o.notes,
            )
            for o in orphan_raw
        ]

        # The v1 `extensions` field lists the installed extension modules by
        # name — the user-declared standalones, excluding the implicit singletons
        # (workspace/product/harness) the dashboard additionally surfaces. This
        # is a pure config read with NO git probe: `ws status` / `--json` must
        # not fail on a broken extension repo just to list its name. The
        # dashboard, which needs each standalone's health, probes them separately
        # via `_collect_standalone_statuses`.
        standalone_repos = self._repo_factory.get_standalone_repos()
        extension_names = [repo.name for repo in standalone_repos]

        # Populate standalone_pins for every declared standalone with a ref.
        # We read the lock file once and do a best-effort HEAD probe per repo —
        # a failed probe yields head_commit=None rather than aborting status.
        standalone_pins = self._collect_standalone_pins(standalone_repos)

        workspace_level = WorkspaceLevelSnapshot(
            root_path=str(self._workspace.root_path),
            extensions=extension_names,
            orphans=orphan_snapshots,
            drift_missing=[r.name for r in drift_report.missing],
            drift_undeclared=list(drift_report.undeclared),
            standalone_pins=standalone_pins,
        )

        # Dashboard layout resolution reflects the whole-workspace shape — every
        # env, and the per-env worktree count — read from the full (pre-pattern)
        # overviews, so the block is stable regardless of `ws status` patterns
        # and matches the TUI grid, which derives the same counts from the same
        # overview structure. `resolve` is the shared policy the grid also calls.
        n_repos = len(overviews[0].repo_statuses) if overviews else 0
        resolved_layout = self._dashboard_layout.resolve(n_repos, len(overviews))
        dashboard = DashboardSnapshot(
            configured_layout=self._dashboard_layout.value,
            resolved_layout=resolved_layout.value,
        )

        return WorkspaceSnapshot(
            schema_version=1,
            workspace=workspace_level,
            environments=env_snapshots,
            source_checkouts=source_checkout_snapshots,
            dashboard=dashboard,
        )

    def collect_for_dashboard(
        self,
        *,
        on_repo_error: Callable[[FeatureWorktree, RepoError], None] | None = None,
        env_decorators: list[IEnvironmentDecorator] | None = None,
        worktree_repo_decorators: list[IWorktreeRepoDecorator] | None = None,
    ) -> DashboardRefreshData:
        """Collect the dashboard-facing state in one pass.

        Returns the same data the dashboard TUI needs to populate all its
        widgets — `FeatureEnvironmentOverview` items for the grid, standalone
        statuses for the singletons table, and main-branch statuses for the
        repo label column — without duplicating the orchestration that lives
        in `EnvStatusService` and the repo repositories.

        This is deliberately separate from `collect()` so the dashboard does
        not pay for the extra per-worktree `get_worktree_status` re-probe that
        `collect()` performs to build `WorktreeSnapshot` fine-grained fields.
        """
        project_repos = self._repo_factory.get_project_repos()

        # ── environments ──────────────────────────────────────────────────
        environments = self._workspace_repo.get_environments(self._workspace, project_repos)

        overviews: list[FeatureEnvironmentOverview] = []
        for env in environments:
            overview = self._collect_env_overview(
                env,
                project_repos,
                on_repo_error=on_repo_error,
                env_decorators=env_decorators,
                worktree_repo_decorators=worktree_repo_decorators,
            )
            if overview is not None:
                overviews.append(overview)

        # ── singletons + standalones ───────────────────────────────────────
        # Dashboard-only: it surfaces the implicit singletons plus the declared
        # standalones, each with its git-probed health. `collect()` does not
        # probe standalones — its `extensions` field is a pure name read.
        standalone_statuses = self._collect_standalone_statuses(
            [*self._repo_factory.get_singleton_repos(), *self._repo_factory.get_standalone_repos()],
            tolerate=on_repo_error is not None,
        )

        # ── main-branch statuses ──────────────────────────────────────────
        main_statuses = self._collect_main_branch_statuses(project_repos, tolerate=on_repo_error is not None)

        return DashboardRefreshData(
            overviews=overviews,
            standalone_statuses=standalone_statuses,
            main_statuses=main_statuses,
        )

    # ── collection helpers ──────────────────────────────────────────────────
    #
    # `_collect_env_overview` and `_collect_main_branch_statuses` are shared by
    # both public methods, so a new field or error policy added to one surface
    # cannot be silently forgotten on the other; they differ only in return
    # shape and a cheap, explicit opt-out (the per-worktree count re-probe in
    # `collect()`, commented at its call site). `_collect_standalone_statuses`
    # is dashboard-only — `collect()`'s `extensions` field is a pure name read
    # that needs no probe (see its call site).
    #
    # The two non-worktree helpers take a `tolerate` flag rather than the
    # `on_repo_error` callback: they operate on `ProjectRepository` /
    # `StandaloneRepository`, not `FeatureWorktree`, so the worktree-typed
    # callback was never invoked with the right object — it served only as a
    # present/absent toggle. Only `_collect_env_overview`, which genuinely routes
    # per-worktree errors, still takes the callback.

    def _collect_env_overview(
        self,
        env: FeatureEnvironment,
        project_repos: list[ProjectRepository],
        *,
        on_repo_error: Callable[[FeatureWorktree, RepoError], None] | None,
        env_decorators: list[IEnvironmentDecorator] | None,
        worktree_repo_decorators: list[IWorktreeRepoDecorator] | None,
    ) -> FeatureEnvironmentOverview | None:
        """Build one env's overview (status + per-repo statuses) under the shared error policy.

        This is the single env-level error-tolerance path: when `on_repo_error`
        is supplied (dashboard), an env-level `RepoError` is logged and the env
        is skipped (returns ``None``); when it is ``None`` (CLI / JSON), the
        error propagates so the command exits non-zero. Per-worktree errors are
        routed to the same callback by `get_worktree_repo_statuses`.
        """

        try:
            env_status = self._env_status_svc.get_environment_status(
                env,
                project_repos,
                env_decorators or None,
            )
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
            # Per-worktree errors flow straight to the caller's callback (it is
            # already worktree-typed); `get_worktree_repo_statuses` skips on a
            # callback and propagates on None — the same policy as the env-level
            # try/except below.
            repo_statuses = self._env_status_svc.get_worktree_repo_statuses(
                env_worktrees,
                worktree_repo_decorators or None,
                on_repo_error=on_repo_error,
            )
        except RepoError as exc:
            if on_repo_error is None:
                raise
            logger.warning("env-level probe failed for %s: %s", env.name, exc)
            return None
        return FeatureEnvironmentOverview(status=env_status, repo_statuses=repo_statuses)

    def _collect_main_branch_statuses(
        self,
        project_repos: list[ProjectRepository],
        *,
        tolerate: bool,
    ) -> dict[str, WorktreeRepoStatus]:
        """Probe each project repo's main-branch checkout under the shared error policy.

        Feeds `collect()`'s source-checkout snapshots and the dashboard's
        repo-label column. When `tolerate` is true (dashboard) a failed probe is
        logged and skipped; when false (CLI / JSON) the first `RepoError`
        propagates.
        """

        def _on_main_error(repo: ProjectRepository, exc: RepoError) -> None:
            logger.warning("main-branch probe failed for %s: %s", repo.name, exc)

        return self._env_status_svc.get_main_branch_statuses(
            self._workspace,
            project_repos,
            on_repo_error=_on_main_error if tolerate else None,
        )

    def _collect_standalone_statuses(
        self,
        repos: list[StandaloneRepository],
        *,
        tolerate: bool,
    ) -> list[StandaloneRepoStatus]:
        """Probe each standalone/singleton repo's status under the shared error policy.

        Dashboard-only: it passes the implicit singletons plus the declared
        standalones for its standalone panel. (`collect()` does not call this —
        its `extensions` field is a pure name read.) When `tolerate` is true a
        failed probe is logged and skipped; when false the first `RepoError`
        propagates — the same propagate-vs-skip contract used for worktree and
        main-branch probes.
        """
        statuses: list[StandaloneRepoStatus] = []
        for repo in repos:
            try:
                statuses.append(self._repo_repo.get_standalone_status(repo))
            except RepoError as exc:
                if not tolerate:
                    raise
                logger.warning("standalone probe failed for %s: %s", repo.name, exc)
        return statuses

    def _collect_standalone_pins(
        self,
        repos: list[StandaloneRepository],
    ) -> list[StandalonePinSnapshot]:
        """Build pin/lock snapshots for every declared standalone that has a ``ref``.

        Reads the lock file once, then for each pinned standalone does a
        best-effort HEAD commit probe (logs and continues on failure rather than
        aborting the whole status command).
        """
        pinned = [r for r in repos if r.ref is not None]
        if not pinned:
            return []

        try:
            lock_entries = self._config_lock_repo.read()
        except Exception as exc:
            logger.warning("could not read config.lock for status: %s", exc)
            lock_entries = {}

        snapshots: list[StandalonePinSnapshot] = []
        for repo in pinned:
            assert repo.ref is not None  # narrowed above
            lock = lock_entries.get(repo.name)

            head_commit: str | None = None
            try:
                head_commit = self._git_repo.get_head_commit(repo.path)
            except Exception as exc:
                logger.debug("HEAD probe failed for %s: %s", repo.name, exc)

            config_ref_drift = lock is not None and lock.ref != repo.ref
            head_drift = lock is not None and head_commit is not None and head_commit != lock.commit

            snapshots.append(
                StandalonePinSnapshot(
                    name=repo.name,
                    ref=repo.ref,
                    kind=lock.kind.value if lock is not None else None,
                    locked_commit=lock.commit if lock is not None else None,
                    config_ref_drift=config_ref_drift,
                    head_drift=head_drift,
                    head_commit=head_commit,
                )
            )
        return snapshots


@dataclasses.dataclass
class DashboardRefreshData:
    """Data returned by `WorkspaceSnapshotService.collect_for_dashboard()`.

    Carries exactly the objects the dashboard TUI widgets need:

    - ``overviews`` — one `FeatureEnvironmentOverview` per discovered env,
      fed to `FeatureWorktreesGrid.statuses` and `ServicePanel.statuses`.
    - ``standalone_statuses`` — singletons + standalones, fed to
      `StandaloneReposTable.statuses`.
    - ``main_statuses`` — keyed by repo name, fed to
      `FeatureWorktreesGrid.main_statuses` for the repo-label column.
    """

    overviews: list[FeatureEnvironmentOverview]
    standalone_statuses: list[StandaloneRepoStatus]
    main_statuses: dict[str, WorktreeRepoStatus]
