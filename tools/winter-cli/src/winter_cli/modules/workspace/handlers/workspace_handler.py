from __future__ import annotations

import dataclasses
import enum
import json
import sys
from typing import Any

import click

from winter_cli.core.cli_output_service import Cell, ICliOutputService
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.env_index import resolve_env_index
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.models import (
    CheckoutResult,
    DiffMode,
    EnvCheckoutReport,
    FeatureEnvironmentOverview,
    FeatureEnvironmentStatus,
    MergeMode,
    PinnedScope,
    PullMode,
    PushReport,
    RepoScope,
    SyncResult,
    Workspace,
    WorktreeRepoStatus,
)
from winter_cli.modules.workspace.prune_service import PruneOrphan, PruneService
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_merge_service import WorkspaceMergeService
from winter_cli.modules.workspace.workspace_push_service import WorkspacePushService
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService


@dataclasses.dataclass
class EnvListParams:
    output_json: bool


@dataclasses.dataclass
class EnvStatusParams:
    env: str | None
    output_json: bool


@dataclasses.dataclass
class EnvSyncParams:
    env: str
    output_json: bool


@dataclasses.dataclass
class EnvConnectParams:
    env: str
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
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        if params.env:
            env = self._workspace_repo.get_environment(self._workspace, params.env)
            env_status = self._workspace_repo.get_environment_status(env, project_repos)
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
            repo_statuses = self._env_status_svc.get_worktree_repo_statuses(env_worktrees)
            self._render_single(env_status, repo_statuses, params.output_json)
        else:
            environments = self._workspace_repo.get_environments(self._workspace, project_repos)
            overviews = []
            for env in environments:
                env_status = self._workspace_repo.get_environment_status(env, project_repos)
                env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
                repo_statuses = self._env_status_svc.get_worktree_repo_statuses(env_worktrees)
                overviews.append(FeatureEnvironmentOverview(status=env_status, repo_statuses=repo_statuses))
            self._render_grid(overviews, params.output_json)

    def sync(self, params: EnvSyncParams) -> None:
        env = self._workspace_repo.get_environment(self._workspace, params.env)
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        report = self._workspace_sync_svc.sync_env(env_worktrees)

        if params.output_json:
            _echo_json(_to_dict(report))
            if not report.success:
                sys.exit(1)
            return

        rows: list[list[str | Cell]] = []
        row_styles: list[str | None] = []
        for outcome in report.repos:
            result_val = outcome.sync_result.value

            if result_val == "fast_forwarded":
                style = "green"
                notes = ""
            elif result_val == "up_to_date":
                style = "dim"
                notes = ""
            elif result_val == "merged":
                style = "cyan"
                notes = "merge commit created"
            else:
                style = "yellow"
                notes = f"+{outcome.ahead} / -{outcome.behind}"

            rows.append([outcome.repo_name, result_val, notes])
            row_styles.append(style)

        for line in self._cli_output_svc.render_table(rows, headers=["REPO", "RESULT", "NOTES"], row_styles=row_styles):
            click.echo(line)

        out = self._cli_output_svc
        if report.success:
            click.echo(f"\n{out.style('✓', 'green')} {report.env} synced successfully")
        else:
            click.echo(f"\n{out.style('!', 'yellow')} {report.env} has diverged repos")
            sys.exit(1)

    def connect(self, params: EnvConnectParams) -> None:
        env = self._workspace_repo.get_environment(self._workspace, params.env)
        project_repos = self._repo_factory.get_project_repos()
        self._drift_warning_svc.raise_warning()
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        count = self._env_checkout_svc.connect_env(env_worktrees, params.feature_branch)

        if params.output_json:
            _echo_json(
                {
                    "env": params.env,
                    "feature_branch": params.feature_branch,
                    "repos_configured": count,
                }
            )
            return

        out = self._cli_output_svc
        click.echo(
            f"{out.style('✓', 'green')} Connected "
            f"{out.style(params.env, 'bold')} → "
            f"{out.style(params.feature_branch, 'bold')} ({count} repos)"
        )

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
            if outcome.result == CheckoutResult.reset:
                style = "green"
            elif outcome.result == CheckoutResult.skip_missing_ref:
                style = "dim"
            else:
                style = "red"
            rows.append([outcome.repo_name, outcome.result.value])
            row_styles.append(style)

        for line in out.render_table(rows, headers=["REPO", "RESULT"], row_styles=row_styles):
            click.echo(line)

        if report.aborted:
            click.echo(
                f"\n{out.style('✗', 'red')} {out.style(report.env, 'bold')} "
                f"not checked out — safety gate refused (no changes made). "
                f"Re-run with {out.style('--force', 'bold')} to bypass."
            )
        else:
            reset_count = sum(1 for o in report.repos if o.result == CheckoutResult.reset)
            skip_count = sum(1 for o in report.repos if o.result == CheckoutResult.skip_missing_ref)
            details = [f"{reset_count} reset"]
            if skip_count:
                details.append(f"{skip_count} skipped")
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
        idx = resolve_env_index(params.name)
        if params.output_json:
            _echo_json({"name": params.name, "index": idx})
            return
        click.echo(idx)

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

    def _render_single(
        self,
        env_status: FeatureEnvironmentStatus,
        repo_statuses: list[WorktreeRepoStatus],
        output_json: bool,
    ) -> None:
        if output_json:
            _echo_json({"environment": _to_dict(env_status), "repos": _to_dict(repo_statuses)})
            return

        out = self._cli_output_svc
        click.echo(f"{out.style('Env:', 'bold')} {env_status.environment.name}")
        if env_status.feature_branch:
            click.echo(f"{out.style('Branch:', 'bold')}   {env_status.feature_branch}")
        for key, value in env_status.extensions.items():
            if value:
                click.echo(f"{out.style(key + ':', 'bold')} {value}")
        click.echo()

        if not repo_statuses:
            click.echo(out.style("No repos", "dim"))
            return

        extension_keys: list[str] = []
        for repo_status in repo_statuses:
            for k in repo_status.extensions:
                if k not in extension_keys:
                    extension_keys.append(k)

        headers: list[str | Cell] = ["REPO", "SYNC", "DIRTY", *(k.upper() for k in extension_keys)]
        rows: list[list[str | Cell]] = []
        row_styles: list[str | None] = []

        for repo_status in repo_statuses:
            sync_parts = []
            if repo_status.ahead:
                sync_parts.append(f"+{repo_status.ahead}")
            if repo_status.behind:
                sync_parts.append(f"-{repo_status.behind}")
            sync_str = ", ".join(sync_parts) if sync_parts else ""

            if repo_status.dirty_count == 0:
                dirty_str = ""
            elif repo_status.dirty_count == 1:
                dirty_str = "1 file"
            else:
                dirty_str = f"{repo_status.dirty_count} files"

            if repo_status.dirty_count:
                row_style: str | None = "red"
            elif repo_status.ahead and repo_status.behind:
                row_style = "dark_orange"
            elif repo_status.ahead:
                row_style = "green"
            elif repo_status.behind:
                row_style = "yellow"
            else:
                row_style = None

            row: list[str | Cell] = [repo_status.worktree.repository.name, sync_str, dirty_str]
            for key in extension_keys:
                ext = repo_status.extensions.get(key, {})
                row.append(str(ext) if ext else "-")
            rows.append(row)
            row_styles.append(row_style)

        for line in self._cli_output_svc.render_table(rows, headers=headers, row_styles=row_styles):
            click.echo(line)

    def _render_grid(
        self,
        overviews: list[FeatureEnvironmentOverview],
        output_json: bool,
    ) -> None:
        if output_json:
            _echo_json([{"environment": _to_dict(o.status), "repos": _to_dict(o.repo_statuses)} for o in overviews])
            return

        repo_names: list[str] = []
        if overviews:
            repo_names = [rs.worktree.repository.name for rs in overviews[0].repo_statuses]

        # Two header rows: env name (+ badges) on top, feature branch on the bottom.
        headers_top: list[str | Cell] = ["REPO"]
        headers_bottom: list[str | Cell] = [""]
        for overview in overviews:
            badges = " ".join(v for v in overview.status.extensions.values() if v)

            has_ahead = any(rs.ahead for rs in overview.repo_statuses)
            has_behind = any(rs.behind for rs in overview.repo_statuses)

            if has_ahead and has_behind:
                header_color: str | None = "dark_orange"
            elif has_ahead:
                header_color = "green"
            elif has_behind:
                header_color = "yellow"
            else:
                header_color = None

            name_label = overview.status.environment.name.capitalize()
            top_text = f"{name_label} {badges}".rstrip()
            if header_color:
                headers_top.append(Cell.of(top_text, header_color))
            else:
                headers_top.append(top_text)
            branch = overview.status.feature_branch or "—"
            headers_bottom.append(Cell.of(branch, "dim"))

        repo_lookup: dict[str, dict[str, WorktreeRepoStatus]] = {}
        for overview in overviews:
            repo_lookup[overview.status.environment.name] = {
                rs.worktree.repository.name: rs for rs in overview.repo_statuses
            }

        rows: list[list[str | Cell]] = [headers_bottom]
        for repo_name in repo_names:
            row: list[str | Cell] = [repo_name]
            for overview in overviews:
                repo_status = repo_lookup[overview.status.environment.name].get(repo_name)
                if repo_status is None:
                    row.append(Cell.of("-", "dim"))
                    continue
                cell = self._format_cell(repo_status)
                row.append(cell)
            rows.append(row)

        for line in self._cli_output_svc.render_table(rows, headers=headers_top):
            click.echo(line)

    @staticmethod
    def _format_cell(repo_status: WorktreeRepoStatus) -> Cell:
        segments: list[tuple[str, str | None]] = []
        if repo_status.ahead:
            if segments:
                segments.append((" ", None))
            segments.append((f"+{repo_status.ahead}", "green"))
        if repo_status.behind:
            if segments:
                segments.append((" ", None))
            segments.append((f"-{repo_status.behind}", "yellow"))
        if repo_status.dirty_count == 1:
            if segments:
                segments.append((" ", None))
            segments.append(("1 file", "red"))
        elif repo_status.dirty_count > 1:
            if segments:
                segments.append((" ", None))
            segments.append((f"{repo_status.dirty_count} files", "red"))
        if not segments:
            return Cell.of("·", "dim")
        return Cell.compose(segments)


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
