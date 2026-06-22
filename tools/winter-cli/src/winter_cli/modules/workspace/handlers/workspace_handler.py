from __future__ import annotations

import dataclasses
import enum
import json
import sys
from typing import Any

import click

from winter_cli.core.cli_output_service import Cell, ICliOutputService
from winter_cli.modules.service.scope import WORKSPACE_SCOPE
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.env_index import resolve_env_index
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.models import (
    CheckoutResult,
    DiffMode,
    EnvCheckoutReport,
    EnvSnapshot,
    FeatureEnvironment,
    MergeMode,
    PinnedScope,
    ProjectRepository,
    PullMode,
    PushReport,
    RepoError,
    RepoScope,
    SourceCheckoutSnapshot,
    SyncResult,
    Workspace,
    WorkspaceLevelSnapshot,
    WorkspaceSnapshot,
    WorktreeRepoStatus,
    WorktreeSnapshot,
)
from winter_cli.modules.workspace.prune_service import PruneOrphan, PruneService
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_merge_service import WorkspaceMergeService
from winter_cli.modules.workspace.workspace_push_service import WorkspacePushService
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService
from winter_cli.plugins.loader import PluginRegistry


@dataclasses.dataclass
class EnvListParams:
    output_json: bool


@dataclasses.dataclass
class EnvStatusParams:
    patterns: list[str]
    output_json: bool
    fetch: bool = False


@dataclasses.dataclass
class EnvConnectParams:
    patterns: list[str]
    feature_branch: str
    output_json: bool


@dataclasses.dataclass
class EnvDisconnectParams:
    env: str
    output_json: bool


@dataclasses.dataclass
class EnvCheckoutParams:
    env: str
    feature_branch: str
    force: bool
    new: bool
    output_json: bool


@dataclasses.dataclass
class EnvPushParams:
    patterns: list[str]
    scope: RepoScope
    pinned_scope: PinnedScope
    output_json: bool


@dataclasses.dataclass
class EnvFetchParams:
    patterns: list[str]
    scope: RepoScope
    output_json: bool


@dataclasses.dataclass
class EnvPullParams:
    patterns: list[str]
    scope: RepoScope
    mode: PullMode
    autostash: bool
    output_json: bool


@dataclasses.dataclass
class EnvMergeParams:
    source_ref: str
    patterns: list[str]
    scope: RepoScope
    mode: MergeMode
    autostash: bool
    pinned_scope: PinnedScope
    output_json: bool


@dataclasses.dataclass
class EnvUpdateParams:
    repo: str | None
    autostash: bool
    output_json: bool


@dataclasses.dataclass
class EnvDiffParams:
    env: str
    mode: DiffMode
    repo_filter: str | None
    no_headers: bool
    output_json: bool


@dataclasses.dataclass
class EnvIndexParams:
    name: str
    output_json: bool


@dataclasses.dataclass
class WorkspacePruneParams:
    dry_run: bool
    force: bool
    output_json: bool


@dataclasses.dataclass
class EnvWorktreesParams:
    output_json: bool
    with_status: bool = False


# Sentinel label for the implicit workspace repo (the workspace root). It has no
# `<env>/<repo>` location, so it renders under a stable, recognizable name in both
# the `ws worktrees` table and `--json` output (and therefore the neovim picker).
# Derived from WORKSPACE_SCOPE so the canonical spelling is never duplicated.
_WORKSPACE_LABEL = f"<{WORKSPACE_SCOPE}>"


@dataclasses.dataclass(frozen=True)
class WorktreeLocation:
    kind: str
    env: str | None
    repo: str | None
    name: str | None
    label: str
    path: str
    ahead: int | None = None
    behind: int | None = None
    dirty: int | None = None


class WorkspaceHandler:
    def __init__(
        self,
        env_status_svc: EnvStatusService,
        workspace_sync_svc: WorkspaceSyncService,
        workspace_push_svc: WorkspacePushService,
        workspace_merge_svc: WorkspaceMergeService,
        env_checkout_svc: EnvCheckoutService,
        workspace_repo: IReadWorkspaceRepository,
        repo_repo: IReadRepoRepository,
        repo_factory: RepositoryFactory,
        drift_warning_svc: DriftWarningService,
        prune_svc: PruneService,
        reporter_factory: ReporterFactory,
        cli_output_svc: ICliOutputService,
        workspace: Workspace,
        workspace_snapshot_svc: WorkspaceSnapshotService | None = None,
        env_aliases: list[str] | None = None,
        envs_per_workspace: int | None = None,
        env_index_registry: IEnvIndexRegistry | None = None,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self._env_status_svc = env_status_svc
        self._workspace_sync_svc = workspace_sync_svc
        self._workspace_push_svc = workspace_push_svc
        self._workspace_merge_svc = workspace_merge_svc
        self._env_checkout_svc = env_checkout_svc
        self._workspace_repo = workspace_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._drift_warning_svc = drift_warning_svc
        self._prune_svc = prune_svc
        self._reporter_factory = reporter_factory
        self._cli_output_svc = cli_output_svc
        self._workspace = workspace
        self._workspace_snapshot_svc = workspace_snapshot_svc
        self._env_aliases = env_aliases
        self._envs_per_workspace = envs_per_workspace
        self._env_index_registry = env_index_registry
        self._plugin_registry = plugin_registry

    def list(self, params: EnvListParams) -> None:
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        environments = self._workspace_repo.get_environments(self._workspace, project_repos)
        statuses = [self._workspace_repo.get_environment_status(env, project_repos) for env in environments]

        if params.output_json:
            items = [_to_dict(s) for s in statuses]
            _echo_json(items)
            return

        rows: list[list[str | Cell]] = []
        for s in statuses:
            feature_branch = s.feature_branch or "-"
            status_text = " ".join(v for v in s.extensions.values() if v) or "-"
            rows.append([s.environment.name, feature_branch, status_text])

        for line in self._cli_output_svc.render_table(rows, headers=["ENV", "FEATURE BRANCH", "STATUS"]):
            click.echo(line)

    def status(self, params: EnvStatusParams) -> None:
        if self._workspace_snapshot_svc is None:
            raise click.ClickException("workspace_snapshot_svc not wired — internal configuration error")

        if params.fetch:
            reporter = self._reporter_factory.get_fetch_reporter(params.output_json)
            self._workspace_sync_svc.fetch_all(
                scope=RepoScope.project,
                patterns=params.patterns,
                reporter=reporter,
            )

        if params.output_json and self._plugin_registry:
            env_decorators: list | None = list(self._plugin_registry.environment_decorators)
            worktree_repo_decorators: list | None = list(self._plugin_registry.worktree_repo_decorators)
        else:
            env_decorators = None
            worktree_repo_decorators = None

        try:
            snapshot = self._workspace_snapshot_svc.collect(
                patterns=params.patterns,
                on_repo_error=None,
                env_decorators=env_decorators or None,
                worktree_repo_decorators=worktree_repo_decorators or None,
            )
        except click.ClickException as exc:
            click.echo(f"Error: {exc.format_message()}", err=True)
            sys.exit(2)
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)

        if params.output_json:
            _echo_json(_snapshot_to_dict(snapshot))
        else:
            self._render_status_table(snapshot)

        exit_code = compute_status_exit_code(snapshot, scoped=bool(params.patterns))
        if exit_code != 0:
            sys.exit(exit_code)

    def connect(self, params: EnvConnectParams) -> None:
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        environments = self._envs_for_patterns(params.patterns, project_repos)

        connected: list[tuple[str, str]] = []
        for env in environments:
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
            for repo_name in self._env_checkout_svc.connect_env(env_worktrees, params.feature_branch, params.patterns):
                connected.append((env.name, repo_name))

        if params.output_json:
            _echo_json(
                {
                    "patterns": params.patterns,
                    "feature_branch": params.feature_branch,
                    "connected": [{"env": e, "repo": r} for e, r in connected],
                    "count": len(connected),
                }
            )
            return

        out = self._cli_output_svc
        if not connected:
            click.echo(out.style(f"No worktrees matched: {' '.join(params.patterns)}", "dim"))
            return

        click.echo(
            f"{out.style('✓', 'green')} Connected "
            f"{out.style(str(len(connected)), 'bold')} "
            f"worktree{'s' if len(connected) != 1 else ''} → "
            f"{out.style(params.feature_branch, 'bold')}"
        )
        for env_name, repo_name in connected:
            click.echo(f"  {env_name}/{repo_name}")

    def _envs_for_patterns(
        self, patterns: list[str], project_repos: list[ProjectRepository]
    ) -> list[FeatureEnvironment]:
        """Resolve the environments a set of connect patterns references.

        A literal env segment (no glob char) is resolved by name via
        `get_environment`, so arbitrary / non-Greek env names still connect — the
        worktree filtering in `connect_env` then narrows to the matched repos. A
        glob env segment (e.g. `*/winter`) falls back to discovering existing
        envs, since there's no single name to resolve.
        """
        env_segments = {p.split("/", 1)[0] for p in patterns}
        literal = {s for s in env_segments if not _has_glob(s)}
        envs = [self._workspace_repo.get_environment(self._workspace, name) for name in sorted(literal)]
        if any(_has_glob(s) for s in env_segments):
            discovered = self._workspace_repo.get_environments(self._workspace, project_repos)
            envs.extend(e for e in discovered if e.name not in literal)
        return envs

    def disconnect(self, params: EnvDisconnectParams) -> None:
        env = self._workspace_repo.get_environment(self._workspace, params.env)
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        count = self._env_checkout_svc.disconnect_env(env_worktrees)

        if params.output_json:
            _echo_json({"env": params.env, "repos_configured": count})
            return

        out = self._cli_output_svc
        click.echo(f"{out.style('✓', 'green')} Disconnected {out.style(params.env, 'bold')} ({count} repos)")

    def checkout(self, params: EnvCheckoutParams) -> None:
        env = self._workspace_repo.get_environment(self._workspace, params.env)
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        report = self._env_checkout_svc.checkout_env(
            env_worktrees,
            params.feature_branch,
            params.force,
            new=params.new,
        )

        if params.output_json:
            _echo_json(_to_dict(report))
            if report.aborted:
                sys.exit(1)
            return

        self._render_checkout_report(report)
        if report.aborted:
            sys.exit(1)

    def _render_checkout_report(self, report: EnvCheckoutReport) -> None:
        out = self._cli_output_svc
        rows: list[list[str | Cell]] = []
        row_styles: list[str | None] = []
        for outcome in report.repos:
            style = "green" if outcome.result in (CheckoutResult.reset_feature, CheckoutResult.reset_main) else "red"
            rows.append([outcome.repo_name, outcome.result.value])
            row_styles.append(style)

        for line in out.render_table(rows, headers=["REPO", "RESULT"], row_styles=row_styles):
            click.echo(line)

        if report.aborted:
            kinds = {o.result for o in report.repos}
            if kinds == {CheckoutResult.refused_unknown_branch}:
                hint = (
                    f"origin/{report.feature_branch} doesn't resolve in any repo. "
                    f"Run {out.style('winter ws fetch', 'bold')} first if the branch exists on the remote, "
                    f"or re-run with {out.style('--new', 'bold')} to start it from each repo's origin/<main>."
                )
            elif CheckoutResult.refused_missing_ref in kinds:
                hint = f"Some repos have no local ref to reset to — run {out.style('winter ws fetch', 'bold')} first."
            else:
                hint = f"Re-run with {out.style('--force', 'bold')} to bypass."
            click.echo(
                f"\n{out.style('✗', 'red')} {out.style(report.env, 'bold')} "
                f"not checked out — refused (no changes made). {hint}"
            )
        else:
            feature_count = sum(1 for o in report.repos if o.result == CheckoutResult.reset_feature)
            main_count = sum(1 for o in report.repos if o.result == CheckoutResult.reset_main)
            details = [f"{feature_count} to feature"]
            if main_count:
                details.append(f"{main_count} from main")
            click.echo(
                f"\n{out.style('✓', 'green')} Checked out "
                f"{out.style(report.env, 'bold')} → "
                f"{out.style(report.feature_branch, 'bold')} ({', '.join(details)})"
            )

    def fetch(self, params: EnvFetchParams) -> None:
        self._drift_warning_svc.raise_warning()
        reporter = self._reporter_factory.get_fetch_reporter(params.output_json)
        report = self._workspace_sync_svc.fetch_all(
            scope=params.scope,
            patterns=params.patterns,
            reporter=reporter,
        )

        if params.output_json:
            if not report.success:
                sys.exit(1)
            return

        if not report.success:
            sys.exit(1)
        if not report.projects and not report.standalone:
            click.echo(self._cli_output_svc.style("Nothing to fetch", "dim"))
            return

    def pull(self, params: EnvPullParams) -> None:
        self._drift_warning_svc.raise_warning()
        reporter = self._reporter_factory.get_pull_reporter(params.output_json)
        report = self._workspace_sync_svc.pull_all(
            scope=params.scope,
            patterns=params.patterns,
            mode=params.mode,
            autostash=params.autostash,
            reporter=reporter,
        )

        if params.output_json:
            if not report.success:
                sys.exit(1)
            return

        out = self._cli_output_svc
        if not report.success:
            if params.mode == PullMode.ff_only and any(
                o.sync_result == SyncResult.diverged for env in report.envs for o in env.repos
            ):
                click.echo(out.style("retry with --merge or --rebase, or resolve with raw git", "dim"))
            sys.exit(1)
        if not report.envs and not report.standalone and not report.skipped:
            click.echo(out.style("Nothing to pull", "dim"))
            return

    def update(self, params: EnvUpdateParams) -> None:
        self._drift_warning_svc.raise_warning()
        reporter = self._reporter_factory.get_pull_reporter(params.output_json)
        try:
            report = self._workspace_sync_svc.update_pins(
                repo_name=params.repo,
                autostash=params.autostash,
                reporter=reporter,
            )
        except RepoError as exc:
            raise click.ClickException(str(exc)) from exc

        if params.output_json:
            if not report.success:
                sys.exit(1)
            return

        out = self._cli_output_svc
        if not report.standalone and not report.skipped:
            click.echo(out.style("Nothing to update", "dim"))
            return
        if not report.success:
            sys.exit(1)

    def merge(self, params: EnvMergeParams) -> None:
        self._drift_warning_svc.raise_warning()
        reporter = self._reporter_factory.get_merge_reporter(params.output_json)
        report = self._workspace_merge_svc.merge_all(
            source_ref=params.source_ref,
            scope=params.scope,
            patterns=params.patterns,
            mode=params.mode,
            autostash=params.autostash,
            pinned_scope=params.pinned_scope,
            reporter=reporter,
        )

        if params.output_json:
            if not report.success:
                sys.exit(1)
            return

        out = self._cli_output_svc
        if not report.envs and not report.standalone:
            click.echo(out.style("Nothing to merge", "dim"))
            return
        if not report.success:
            sys.exit(1)

    def push(self, params: EnvPushParams) -> None:
        self._drift_warning_svc.raise_warning()
        report = self._workspace_push_svc.push_all(
            scope=params.scope,
            patterns=params.patterns,
            pinned_scope=params.pinned_scope,
        )

        if params.output_json:
            _echo_json(_to_dict(report))
            if not report.success:
                sys.exit(1)
            return

        self._render_push_report(report, params.scope)
        if not report.success:
            sys.exit(1)

    def index(self, params: EnvIndexParams) -> None:
        """Report the env index for *name*.

        If *name* has a recorded entry in the registry (i.e. the env was
        created via ``winter ws init``), the persisted index is returned and
        is authoritative.

        If *name* is not registered (a hypothetical / not-yet-created env),
        the suggested index from ``resolve_env_index`` is returned instead.
        This suggestion is deterministic for aliases (fixed slots) but may
        shift on actual creation for ad-hoc names, because the allocator
        linear-probes on collision within the hash band.

        ``--json`` output distinguishes the two cases via
        ``"source": "registry"`` (persisted) or ``"source": "suggested"``
        (not yet created; may shift on create for non-alias names).
        """
        # Check registry first.
        if self._env_index_registry is not None:
            persisted = self._env_index_registry.get_index(params.name)
            if persisted is not None:
                if params.output_json:
                    _echo_json({"name": params.name, "index": persisted, "source": "registry"})
                else:
                    click.echo(persisted)
                return

        # Not in registry — return the suggested slot.
        suggested = resolve_env_index(params.name, self._env_aliases, self._envs_per_workspace)
        if params.output_json:
            _echo_json({"name": params.name, "index": suggested, "source": "suggested"})
        else:
            # Surface the caveat for ad-hoc names: aliases have fixed slots, but
            # ad-hoc names may probe on collision at create time.
            aliases = self._env_aliases or []
            if params.name in aliases:
                click.echo(suggested)
            else:
                click.echo(f"{suggested} (suggested; may shift on create)")

    def worktrees(self, params: EnvWorktreesParams) -> None:
        project_repos = self._repo_factory.get_project_repos()
        environments = self._workspace_repo.get_environments(self._workspace, project_repos)
        standalone_repos = self._repo_factory.get_standalone_repos()

        locations: list[WorktreeLocation] = []

        for env in environments:
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)

            # Build a repo-name → status lookup when --status is requested.
            status_by_repo: dict[str, WorktreeRepoStatus] = {}
            if params.with_status:
                repo_statuses = self._env_status_svc.get_worktree_repo_statuses(env_worktrees)
                status_by_repo = {rs.worktree.repository.name: rs for rs in repo_statuses}

            for wt in env_worktrees.worktrees:
                if wt.path.exists() and wt.path.is_dir():
                    if params.with_status:
                        rs = status_by_repo.get(wt.repository.name)
                        ahead: int | None = rs.ahead if rs is not None else None
                        behind: int | None = rs.behind if rs is not None else None
                        dirty: int | None = rs.dirty_count if rs is not None else None
                        locations.append(
                            WorktreeLocation(
                                kind="worktree",
                                env=wt.environment.name,
                                repo=wt.repository.name,
                                name=None,
                                label=f"{wt.environment.name}/{wt.repository.name}",
                                path=str(wt.path),
                                ahead=ahead,
                                behind=behind,
                                dirty=dirty,
                            )
                        )
                    else:
                        locations.append(
                            WorktreeLocation(
                                kind="worktree",
                                env=wt.environment.name,
                                repo=wt.repository.name,
                                name=None,
                                label=f"{wt.environment.name}/{wt.repository.name}",
                                path=str(wt.path),
                            )
                        )

        for standalone in standalone_repos:
            if standalone.path.exists() and standalone.path.is_dir():
                if params.with_status:
                    # Standalone repos have no env feature-branch comparison for
                    # ahead/behind. Dirty count is not available without new git
                    # plumbing, so best-effort: set all three to null.
                    locations.append(
                        WorktreeLocation(
                            kind="standalone",
                            env=None,
                            repo=None,
                            name=standalone.name,
                            label=standalone.name,
                            path=str(standalone.path),
                            ahead=None,
                            behind=None,
                            dirty=None,
                        )
                    )
                else:
                    locations.append(
                        WorktreeLocation(
                            kind="standalone",
                            env=None,
                            repo=None,
                            name=standalone.name,
                            label=standalone.name,
                            path=str(standalone.path),
                        )
                    )

        workspace_repo = self._repo_factory.get_workspace_repo()
        if workspace_repo is not None and workspace_repo.path.exists() and workspace_repo.path.is_dir():
            ahead = behind = dirty = None
            if params.with_status:
                # The workspace root is a real repo on the workspace branch, so —
                # unlike a user-declared standalone — its ahead/behind/dirty are
                # derivable. A failed probe leaves the fields None rather than
                # crashing the whole listing.
                try:
                    ws_status = self._repo_repo.get_standalone_status(workspace_repo)
                except RepoError:
                    ws_status = None
                if ws_status is not None and ws_status.branch is not None:
                    ahead, behind, dirty = ws_status.ahead, ws_status.behind, ws_status.dirty_count
            locations.append(
                WorktreeLocation(
                    kind="workspace",
                    env=None,
                    repo=None,
                    name=workspace_repo.name,
                    label=_WORKSPACE_LABEL,
                    path=str(workspace_repo.path),
                    ahead=ahead,
                    behind=behind,
                    dirty=dirty,
                )
            )

        if params.output_json:
            _echo_json([_worktree_location_to_dict(loc, params.with_status) for loc in locations])
            return

        rows: list[list[str | Cell]] = []
        if params.with_status:
            for loc in locations:
                status_str = _format_worktree_status(loc)
                rows.append([loc.label, loc.kind, loc.path, status_str])
            for line in self._cli_output_svc.render_table(rows, headers=["LABEL", "KIND", "PATH", "STATUS"]):
                click.echo(line)
        else:
            for loc in locations:
                rows.append([loc.label, loc.kind, loc.path])
            for line in self._cli_output_svc.render_table(rows, headers=["LABEL", "KIND", "PATH"]):
                click.echo(line)

    def prune(self, params: WorkspacePruneParams) -> None:
        orphans = self._prune_svc.find_orphans()

        if params.output_json:
            self._prune_json(params, orphans)
            return

        if not orphans:
            click.echo("Nothing to prune. Workspace is clean.")
            self._maybe_reaggregate_excludes(params, removed_any=False)
            return

        for o in orphans:
            click.echo(self._format_orphan_line(o))

        if params.dry_run:
            return

        if not params.force:
            removable = sum(1 for o in orphans if o.safe_to_remove)
            if removable == 0:
                click.echo("\nNothing to remove (all orphans are blocked). Resolve the notes above and re-run.")
                return
            click.confirm(f"\nRemove {removable} orphan(s)?", abort=True)

        removed_any = False
        for o in orphans:
            if not o.safe_to_remove:
                click.echo(f"  skip   {self._relative(o.path)} ({o.notes})")
                continue
            try:
                self._prune_svc.remove_orphan(o)
                click.echo(f"  remove {self._relative(o.path)}")
                removed_any = True
            except (OSError, RuntimeError) as exc:
                click.echo(f"  error  {self._relative(o.path)} ({exc})")

        self._maybe_reaggregate_excludes(params, removed_any=removed_any)

    def _prune_json(self, params: WorkspacePruneParams, orphans: list[PruneOrphan]) -> None:
        results = []
        if params.dry_run:
            for o in orphans:
                results.append(
                    {
                        "kind": o.kind,
                        "path": str(o.path),
                        "safe_to_remove": o.safe_to_remove,
                        "notes": o.notes,
                        "action": "would_remove" if o.safe_to_remove else "skipped",
                    }
                )
            _echo_json({"dry_run": True, "orphans": results})
            return

        removed_any = False
        for o in orphans:
            entry = {
                "kind": o.kind,
                "path": str(o.path),
                "safe_to_remove": o.safe_to_remove,
                "notes": o.notes,
            }
            if not o.safe_to_remove:
                entry["action"] = "skipped"
            else:
                try:
                    self._prune_svc.remove_orphan(o)
                    entry["action"] = "removed"
                    removed_any = True
                except (OSError, RuntimeError) as exc:
                    entry["action"] = "error"
                    entry["error"] = str(exc)
            results.append(entry)

        excludes_updated = False
        if removed_any:
            excludes_updated = self._prune_svc.reaggregate_excludes(self._reporter_factory.get_init_reporter(True))
        _echo_json({"dry_run": False, "orphans": results, "excludes_updated": excludes_updated})

    def _maybe_reaggregate_excludes(self, params: WorkspacePruneParams, removed_any: bool) -> None:
        if params.dry_run:
            return
        if not removed_any:
            return
        reporter = self._reporter_factory.get_init_reporter(False)
        self._prune_svc.reaggregate_excludes(reporter)

    @staticmethod
    def _format_orphan_line(o: PruneOrphan) -> str:
        marker = " " if o.safe_to_remove else "!"
        suffix = f"  ({o.notes})" if o.notes else ""
        return f"{marker} {o.kind:<18} {o.path}{suffix}"

    def _relative(self, path) -> str:
        try:
            return str(path.relative_to(self._workspace.root_path))
        except ValueError:
            return str(path)

    def diff(self, params: EnvDiffParams) -> None:
        env = self._workspace_repo.get_environment(self._workspace, params.env)
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        result = self._env_status_svc.get_env_diff(env_worktrees, params.mode, repo_filter=params.repo_filter)

        if params.output_json:
            data = {
                "env": result.env,
                "mode": result.mode.value,
                "repos": [
                    {
                        "name": r.repo_name,
                        "files_changed": r.files_changed,
                        "insertions": r.insertions,
                        "deletions": r.deletions,
                    }
                    for r in result.repos
                ],
            }
            _echo_json(data)
            return

        if not result.repos:
            return

        for i, repo in enumerate(result.repos):
            if not params.no_headers:
                if result.mode == DiffMode.branch and repo.ahead:
                    commit_word = "commit" if repo.ahead == 1 else "commits"
                    click.echo(f"=== {repo.repo_name} (+{repo.ahead} {commit_word}) ===")
                else:
                    click.echo(f"=== {repo.repo_name} ===")
            click.echo(repo.diff_text)
            if i < len(result.repos) - 1:
                click.echo()

    def _render_push_report(self, report: PushReport, scope: RepoScope) -> None:
        out = self._cli_output_svc
        sectioned = self._is_sectioned(envs=len(report.envs), include_standalone=scope.includes_standalone)

        any_pushed = any(r for env in report.envs for r in env.repos) or bool(report.standalone)
        if not any_pushed and not report.skipped:
            click.echo(out.style("No repos with commits to push", "dim"))
            return

        for env_report in report.envs:
            if sectioned:
                click.echo(out.style(env_report.env, "bold"))
            if env_report.repos:
                for line in self._push_table_lines(env_report.repos):
                    click.echo(line)
            else:
                click.echo(out.style("(nothing to push)", "dim"))
            if sectioned:
                click.echo()

        for skip in report.skipped:
            click.echo(f"{out.style('!', 'yellow')} {skip.env}: {skip.reason}")

        if scope.includes_standalone:
            if sectioned:
                click.echo(out.style("standalone", "bold"))
            if report.standalone:
                for line in self._push_table_lines(report.standalone):
                    click.echo(line)
            else:
                click.echo(out.style("No standalone repos to push", "dim"))

        if report.success:
            click.echo(f"\n{out.style('✓', 'green')} push complete")
        else:
            click.echo(f"\n{out.style('!', 'yellow')} push had errors or skipped envs")

    @staticmethod
    def _is_sectioned(envs: int, include_standalone: bool) -> bool:
        return envs > 1 or (envs > 0 and include_standalone)

    def _push_table_lines(self, rows) -> list[str]:
        table_rows: list[list[str | Cell]] = []
        for r in rows:
            pushed_cell = Cell.of("yes", "green") if r.pushed else Cell.of("failed", "red")
            commits = str(r.commits) if r.pushed else (r.error or "")
            table_rows.append([r.repo_name, pushed_cell, commits])
        return self._cli_output_svc.render_table(table_rows, headers=["REPO", "PUSHED", "COMMITS"])

    def _render_status_table(self, snapshot: WorkspaceSnapshot) -> None:
        """Render the human-readable `ws status` output from a WorkspaceSnapshot."""
        out = self._cli_output_svc

        # ── environments ──────────────────────────────────────────────────────
        for env_snap in snapshot.environments:
            click.echo(f"{out.style('Env:', 'bold')} {env_snap.name}")
            if env_snap.feature_branch:
                click.echo(f"{out.style('Branch:', 'bold')}   {env_snap.feature_branch}")
            click.echo()

            if not env_snap.worktrees:
                click.echo(out.style("  No repos", "dim"))
                click.echo()
                continue

            rows: list[list[str | Cell]] = []
            row_styles: list[str | None] = []
            for wt in env_snap.worktrees:
                sync_parts: list[str] = []
                if wt.ahead:
                    sync_parts.append(f"+{wt.ahead}")
                if wt.behind:
                    sync_parts.append(f"-{wt.behind}")
                sync_str = ", ".join(sync_parts) if sync_parts else ""

                if wt.dirty == 0:
                    dirty_str = ""
                elif wt.dirty == 1:
                    dirty_str = "1 file"
                else:
                    dirty_str = f"{wt.dirty} files"

                if wt.dirty:
                    row_style: str | None = "red"
                elif wt.ahead and wt.behind:
                    row_style = "dark_orange"
                elif wt.ahead:
                    row_style = "green"
                elif wt.behind:
                    row_style = "yellow"
                else:
                    row_style = None

                rows.append([wt.repo, sync_str, dirty_str])
                row_styles.append(row_style)

            for line in out.render_table(rows, headers=["REPO", "SYNC", "DIRTY"], row_styles=row_styles):
                click.echo(line)
            click.echo()

        # ── source checkouts ──────────────────────────────────────────────────
        if snapshot.source_checkouts:
            click.echo(out.style("Source checkouts:", "bold"))
            sc_rows: list[list[str | Cell]] = []
            sc_styles: list[str | None] = []
            for sc in snapshot.source_checkouts:
                sc_sync_parts: list[str] = []
                if sc.ahead_origin:
                    sc_sync_parts.append(f"+{sc.ahead_origin}")
                if sc.behind_origin:
                    sc_sync_parts.append(f"-{sc.behind_origin}")
                sc_sync_str = ", ".join(sc_sync_parts) if sc_sync_parts else ""
                drift_str = "; ".join(sc.drift) if sc.drift else ""

                if sc.behind_origin or sc.drift:
                    sc_style: str | None = "yellow"
                elif sc.ahead_origin:
                    sc_style = "green"
                else:
                    sc_style = None

                sc_rows.append([sc.repo, sc.branch or "-", sc_sync_str, drift_str])
                sc_styles.append(sc_style)

            for line in out.render_table(sc_rows, headers=["REPO", "BRANCH", "SYNC", "DRIFT"], row_styles=sc_styles):
                click.echo(line)
            click.echo()

        # ── workspace-level ───────────────────────────────────────────────────
        ws = snapshot.workspace
        has_workspace_issues = ws.orphans or ws.drift_missing or ws.drift_undeclared
        if not has_workspace_issues:
            return

        click.echo(out.style("Workspace:", "bold"))
        if ws.drift_missing:
            click.echo(f"  {out.style('missing:', 'yellow')} {', '.join(ws.drift_missing)}")
        if ws.drift_undeclared:
            click.echo(f"  {out.style('undeclared:', 'yellow')} {', '.join(ws.drift_undeclared)}")
        if ws.orphans:
            click.echo(f"  {out.style('orphans:', 'yellow')} {len(ws.orphans)}")
            for o in ws.orphans:
                marker = "  " if o.safe_to_remove else "! "
                click.echo(f"    {marker}{o.kind}  {o.path}")
        click.echo()


def _has_glob(segment: str) -> bool:
    """Whether a pattern segment contains an fnmatch wildcard (`*`, `?`, `[`)."""
    return any(c in segment for c in "*?[")


def compute_status_exit_code(snapshot: WorkspaceSnapshot, *, scoped: bool) -> int:
    """Compute the exit code for `ws status`.

    Contract (locked):
      0 = clean
      1 = dirty OR drifted
      2 = command error  (callers map exceptions → 2 before reaching here)

    When ``scoped`` is True (patterns were given), only the already-filtered
    snapshot's worktree dirtiness is considered for the exit code.  Global
    source-checkout drift, config drift, and orphans are still rendered as
    context but do NOT flip the exit code.

    When ``scoped`` is False (no patterns, full workspace) the full workspace is
    considered: any dirty worktree OR any source-checkout drift (behind_origin >
    0 or ahead_origin > 0 or non-empty drift list) OR any orphans OR any config
    drift counts as ``1``.
    """
    if scoped:
        # Scoped: only the matched worktrees contribute to dirtiness.
        for env_snap in snapshot.environments:
            for wt in env_snap.worktrees:
                if wt.dirty > 0:
                    return 1
        return 0

    # Unscoped: check everything.
    for env_snap in snapshot.environments:
        for wt in env_snap.worktrees:
            if wt.dirty > 0:
                return 1

    for sc in snapshot.source_checkouts:
        if sc.behind_origin > 0 or sc.ahead_origin > 0 or sc.dirty > 0 or sc.drift:
            return 1

    ws = snapshot.workspace
    if ws.orphans or ws.drift_missing or ws.drift_undeclared:
        return 1

    return 0


def _snapshot_to_dict(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    """Serialize a WorkspaceSnapshot to a stable v1 dict (snake_case, schema_version=1)."""
    return {
        "schema_version": snapshot.schema_version,
        "environments": [_env_snap_to_dict(e) for e in snapshot.environments],
        "source_checkouts": [_sc_snap_to_dict(sc) for sc in snapshot.source_checkouts],
        "workspace": _workspace_level_to_dict(snapshot.workspace),
        "dashboard": {
            "configured_layout": snapshot.dashboard.configured_layout,
            "resolved_layout": snapshot.dashboard.resolved_layout,
        },
    }


def _env_snap_to_dict(env: EnvSnapshot) -> dict[str, Any]:
    return {
        "name": env.name,
        "index": env.index,
        "port_base": env.port_base,
        "feature_branch": env.feature_branch,
        "worktrees": [_worktree_snap_to_dict(wt) for wt in env.worktrees],
        "extensions": dict(env.extensions),
    }


def _worktree_snap_to_dict(wt: WorktreeSnapshot) -> dict[str, Any]:
    return {
        "repo": wt.repo,
        "branch": wt.branch,
        "upstream": wt.upstream,
        "ahead": wt.ahead,
        "behind": wt.behind,
        "tracking_ahead": wt.tracking_ahead,
        "tracking_behind": wt.tracking_behind,
        "tracking_ref_present": wt.tracking_ref_present,
        "staged": wt.staged,
        "unstaged": wt.unstaged,
        "untracked": wt.untracked,
        "dirty": wt.dirty,
        "last_commit_subject": wt.last_commit_subject,
        "pinned": wt.pinned,
        "main_branch": wt.main_branch,
    }


def _sc_snap_to_dict(sc: SourceCheckoutSnapshot) -> dict[str, Any]:
    return {
        "repo": sc.repo,
        "branch": sc.branch,
        "behind_origin": sc.behind_origin,
        "ahead_origin": sc.ahead_origin,
        "dirty": sc.dirty,
        "drift": list(sc.drift),
    }


def _workspace_level_to_dict(ws: WorkspaceLevelSnapshot) -> dict[str, Any]:
    return {
        "root_path": ws.root_path,
        "extensions": list(ws.extensions),
        "orphans": [
            {
                "kind": o.kind,
                "path": o.path,
                "safe_to_remove": o.safe_to_remove,
                "notes": o.notes,
            }
            for o in ws.orphans
        ],
        "drift_missing": list(ws.drift_missing),
        "drift_undeclared": list(ws.drift_undeclared),
        "standalone_pins": [
            {
                "name": p.name,
                "ref": p.ref,
                "kind": p.kind,
                "locked_commit": p.locked_commit,
                "config_ref_drift": p.config_ref_drift,
                "head_drift": p.head_drift,
                "head_commit": p.head_commit,
            }
            for p in ws.standalone_pins
        ],
    }


def _worktree_location_to_dict(loc: WorktreeLocation, with_status: bool) -> dict[str, Any]:
    """Serialize a WorktreeLocation to a dict.

    When with_status is False the three status keys (ahead/behind/dirty) are
    completely absent — not present-as-null — so the no-status JSON shape is
    byte-for-byte stable for consumers that key on field presence.
    """
    d: dict[str, Any] = {
        "kind": loc.kind,
        "env": loc.env,
        "repo": loc.repo,
        "name": loc.name,
        "label": loc.label,
        "path": loc.path,
    }
    if with_status:
        d["ahead"] = loc.ahead
        d["behind"] = loc.behind
        d["dirty"] = loc.dirty
    return d


def _format_worktree_status(loc: WorktreeLocation) -> str:
    """Render a short status string for the human table STATUS column."""
    ahead = loc.ahead
    behind = loc.behind
    dirty = loc.dirty if loc.dirty is not None else 0
    if ahead is None and behind is None and dirty == 0:
        return "="
    parts: list[str] = []
    if ahead is not None:
        parts.append(f"+{ahead}")
    if behind is not None:
        parts.append(f"-{behind}")
    if dirty:
        parts.append(f"[+{dirty}]")
    return " ".join(parts) if parts else "="


def _to_dict(obj: Any) -> Any:
    if isinstance(obj, enum.Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, default=str, indent=2))
