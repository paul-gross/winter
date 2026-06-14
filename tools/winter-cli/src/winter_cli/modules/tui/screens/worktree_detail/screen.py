from __future__ import annotations

import contextlib
from typing import cast

from rich.text import Text
from textual import work
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.tui.keybindings import KeybindingMixin, KeybindingResolver, plugin_action_bindings
from winter_cli.modules.tui.keybindings.actions import WORKTREE_DETAIL_ACTIONS
from winter_cli.modules.tui.screens.plugin_action_mixin import PluginActionMixin
from winter_cli.modules.tui.screens.workspace.repo_status import render_repo_cell
from winter_cli.modules.tui.widgets.refresh_status import RefreshStatus
from winter_cli.modules.tui.widgets.repo_detail_view import PanelOutcome, RepoDetailView, render_detail_panels
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.models import (
    FeatureEnvironmentStatus,
    RepoError,
    RepoStatus,
    Workspace,
    WorktreeRepoStatus,
)
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import (
    ActionScope,
    DetailPanelContext,
    FeatureEnvironmentContext,
    FeatureWorktreeContext,
    WorkspaceContext,
)


class WorktreeDetailScreen(KeybindingMixin, PluginActionMixin, Screen):
    # Bindings are installed in on_mount from config-resolved action ids
    # (keybindings.actions.WORKTREE_DETAIL_ACTIONS), not hardcoded here.

    def __init__(
        self,
        worktree_name: str,
        env_status_svc: EnvStatusService,
        workspace_repo: IReadWorkspaceRepository,
        repo_repo: IReadRepoRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        plugin_registry: PluginRegistry,
        error_log: ErrorLogService,
        keybinding_resolver: KeybindingResolver,
        focused_repo: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.worktree_name = worktree_name
        self._env_status_svc = env_status_svc
        self._workspace_repo = workspace_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._plugin_registry = plugin_registry
        self._error_log = error_log
        self._keybinding_resolver = keybinding_resolver
        self._detail_panels = list(plugin_registry.detail_panels)
        self._env_status: FeatureEnvironmentStatus | None = None
        self._repo_statuses: list[WorktreeRepoStatus] = []
        self._focused_repo: str | None = focused_repo
        self._repo_detail: RepoStatus | None = None
        self._detail_repo_keys: list[str] = []

    def compose(self):
        yield Header()
        with Horizontal(id="detail-title-bar"):
            yield Static(id="detail-header")
            yield RefreshStatus(id="refresh-status")
        yield DataTable(id="detail-repos")
        with Horizontal(id="detail-bottom"):
            yield RepoDetailView(self._detail_panels, id="detail-info")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.cursor_type = "row"

        plugin_bindings = plugin_action_bindings(self._plugin_registry, tuple(ActionScope))
        for message in self._install_keybindings([*WORKTREE_DETAIL_ACTIONS, *plugin_bindings]):
            self.app.notify(message, title="keybindings", severity="error", timeout=8)

        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    @work(thread=True)
    def _refresh_data(self) -> None:
        self.app.call_from_thread(self._on_refresh_start)
        worktree_repo_decorators = list(self._plugin_registry.worktree_repo_decorators)
        environment_decorators = list(self._plugin_registry.environment_decorators)
        try:
            project_repos = self._repo_factory.get_project_repos()
            env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
            env_status = self._env_status_svc.get_environment_status(env, project_repos, environment_decorators or None)
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        except RepoError as exc:
            self._capture_error(f"WorktreeDetailScreen({self.worktree_name}).refresh", exc)
            self.app.call_from_thread(self._on_refresh_finished)
            return

        def _on_repo_error(wt, exc):
            self._capture_error(
                f"WorktreeDetailScreen({self.worktree_name}).refresh({wt.repository.name})",
                exc,
            )

        repo_statuses = self._env_status_svc.get_worktree_repo_statuses(
            env_worktrees,
            worktree_repo_decorators or None,
            on_repo_error=_on_repo_error,
        )
        self.app.call_from_thread(self._update_widgets, env_status, repo_statuses)

    def _on_refresh_finished(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    def action_open_log(self) -> None:
        from winter_cli.modules.tui.app import WinterDashboardApp

        app = cast(WinterDashboardApp, self.app)
        app.push_screen(app.screen_factory.error_log_screen())

    def _on_refresh_start(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#refresh-status", RefreshStatus).start_refresh()

    def _update_widgets(
        self,
        env_status: FeatureEnvironmentStatus,
        repo_statuses: list[WorktreeRepoStatus],
    ) -> None:
        self._env_status = env_status
        self._repo_statuses = repo_statuses

        header = self.query_one("#detail-header", Static)
        badges = " ".join(v for v in env_status.extensions.values() if v)
        branch_text = env_status.feature_branch_label(disconnected="disconnected")
        name = env_status.environment.name.capitalize()
        title = f"{name} {badges}".rstrip()
        header.update(f"  {title}  {branch_text}")

        table = self.query_one("#detail-repos", DataTable)
        repo_keys = [rs.worktree.repository.name for rs in repo_statuses]
        structure_matches = repo_keys == self._detail_repo_keys

        if not structure_matches:
            table.clear(columns=True)
            table.add_column("Repo", key="repo")
            table.add_column("Branch", key="branch")
            table.add_column("Status", key="status")
            table.add_column("", key="ext")
            self._detail_repo_keys = repo_keys

            for rs in repo_statuses:
                name = rs.worktree.repository.name
                table.add_row(
                    name,
                    rs.branch or "—",
                    render_repo_cell(rs, include_extensions=False),
                    self._render_extensions(rs),
                    key=name,
                )
        else:
            for rs in repo_statuses:
                name = rs.worktree.repository.name
                table.update_cell(name, "branch", rs.branch or "—")
                table.update_cell(name, "status", render_repo_cell(rs, include_extensions=False))
                table.update_cell(name, "ext", self._render_extensions(rs), update_width=True)
            table._update_count += 1
            table.refresh()

        # Seed/repair the focused repo: fall back to the first repo when none
        # was supplied or the supplied one isn't present in this env.
        if repo_keys and self._focused_repo not in repo_keys:
            self._focused_repo = repo_keys[0]

        # On first render, place the cursor on the focused repo's row so the
        # detail table opens on the repo the matrix cursor was on.
        if not structure_matches and self._focused_repo in repo_keys:
            table.move_cursor(row=repo_keys.index(self._focused_repo), animate=False)

        if self._focused_repo is not None:
            self._load_repo_detail(self._focused_repo)

        self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    @staticmethod
    def _render_extensions(rs: WorktreeRepoStatus) -> Text:
        if not rs.extensions:
            return Text("")
        text = Text()
        first = True
        for key, value in rs.extensions.items():
            if key.startswith("_"):
                continue
            if not first:
                text.append(" ")
            first = False
            if isinstance(value, Text):
                text.append(value)
            else:
                badge = str(value) if value else key
                text.append(badge, style="cyan")
        return text

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None:
            self._focused_repo = str(event.row_key.value)
            self._load_repo_detail(self._focused_repo)

    @work(thread=True)
    def _load_repo_detail(self, repo_name: str) -> None:
        try:
            project_repos = self._repo_factory.get_project_repos()
            env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
            env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
            wt = next(wt for wt in env_worktrees.worktrees if wt.repository.name == repo_name)
            detail = self._repo_repo.get_worktree_status(wt)
        except RepoError as exc:
            self._capture_error(
                f"WorktreeDetailScreen({self.worktree_name}).load_repo_detail({repo_name})",
                exc,
            )
            return
        # Panel rendering is pure and isolated, so it runs here in the worker
        # thread alongside the git read; the UI thread only applies the results.
        outcomes = render_detail_panels(self._detail_panels, DetailPanelContext(worktree=wt))
        self.app.call_from_thread(self._update_repo_info, detail, outcomes)

    def _update_repo_info(self, detail: RepoStatus, outcomes: list[PanelOutcome]) -> None:
        self._repo_detail = detail
        self.query_one("#detail-info", RepoDetailView).show_repo(detail, outcomes)

    def action_cursor_up(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.action_cursor_up()

    def action_cursor_down(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.action_cursor_down()

    def action_cursor_left(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.action_cursor_left()

    def action_cursor_right(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.action_cursor_right()

    def action_refresh(self) -> None:
        self._refresh_data()

    def _run_plugin_action(self, action_name: str) -> None:
        action = next(
            (a for a in self._plugin_registry.tui_actions if a.name == action_name),
            None,
        )
        if action is None:
            return

        if action.scope == ActionScope.workspace:
            self._execute_workspace_action(action_name)
        elif action.scope == ActionScope.feature_environment:
            self._execute_environment_action(action_name)
        elif action.scope == ActionScope.feature_worktree and self._focused_repo is not None:
            self._execute_worktree_action(action_name, self._focused_repo)

    @work(thread=True)
    def _execute_workspace_action(self, action_name: str) -> None:
        ctx = WorkspaceContext(workspace=self._workspace, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.workspace):
            if action.name == action_name:
                action.handler(ctx)
                return

    @work(thread=True)
    def _execute_environment_action(self, action_name: str) -> None:
        env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
        ctx = FeatureEnvironmentContext(environment=env, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.feature_environment):
            if action.name == action_name:
                action.handler(ctx)
                return

    @work(thread=True)
    def _execute_worktree_action(self, action_name: str, repo_name: str) -> None:
        project_repos = self._repo_factory.get_project_repos()
        env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
        env_worktrees = self._env_status_svc.get_feature_environment_worktrees(env, project_repos)
        wt = next(wt for wt in env_worktrees.worktrees if wt.repository.name == repo_name)
        ctx = FeatureWorktreeContext(worktree=wt, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.feature_worktree):
            if action.name == action_name:
                action.handler(ctx)
                return

    def action_back(self) -> None:
        self.app.pop_screen()
