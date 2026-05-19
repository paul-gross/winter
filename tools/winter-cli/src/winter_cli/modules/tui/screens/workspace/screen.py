from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.binding import Binding
from textual.screen import Screen
from textual.containers import Center, Horizontal, Middle
from textual.widgets import Footer, Header, LoadingIndicator, Static

from winter_cli.modules.workspace.models import (
    FeatureEnvironmentOverview,
    FeatureEnvironmentWorktrees,
    StandaloneRepoStatus,
    Workspace,
)
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_service import WorkspaceService
from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import (
    ActionScope,
    FeatureEnvironmentContext,
    FeatureWorktreeContext,
    WorkspaceContext,
)
from winter_cli.modules.tui.widgets.refresh_status import RefreshStatus
from winter_cli.modules.tui.widgets.service_panel import ServicePanel
from winter_cli.modules.tui.screens.workspace.standalone_repos import StandaloneReposTable
from winter_cli.modules.tui.screens.workspace.feature_worktrees import FeatureWorktreesGrid

if TYPE_CHECKING:
    from winter_cli.modules.tui.app import WinterDashboardApp


class WorkspaceScreen(Screen):

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("s", "sync", "Sync"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+k", "jump_prev", "Jump prev", show=False),
        Binding("ctrl+j", "jump_next", "Jump next", show=False),
    ]

    def __init__(
        self,
        workspace_svc: WorkspaceService,
        workspace_repo: IReadWorkspaceRepository,
        repo_repo: IReadRepoRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        plugin_registry: PluginRegistry,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._workspace_svc = workspace_svc
        self._workspace_repo = workspace_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._plugin_registry = plugin_registry
        self._env_worktrees: dict[str, FeatureEnvironmentWorktrees] = {}

    def compose(self):
        yield Header()
        with Middle(id="loading-container"):
            with Center():
                yield Static("Checking git status...", id="loading-label")
            with Center():
                yield LoadingIndicator(id="loading")
        yield Static("[bold]Standalone Repositories[/bold]", id="singletons-label")
        yield StandaloneReposTable(id="singletons")
        yield Static("[bold]Feature Repositories[/bold]", id="grid-label")
        yield FeatureWorktreesGrid(id="grid")
        with Horizontal(id="status-bar"):
            yield Static(
                "[green]+N[/green] [dim]ahead of main[/dim]  "
                "[yellow]-N[/yellow] [dim]behind main[/dim]  "
                "[red]N files[/red] [dim]uncommitted[/dim]  "
                "[cyan]\\[+N][/cyan] [dim]unpushed to tracking[/dim]  "
                "[dark_orange]\\[+][/dark_orange] [dim]upstream not pushed yet[/dim]",
                id="legend",
            )
            yield RefreshStatus(id="refresh-status")
        yield ServicePanel(id="services")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#singletons-label").display = False
        self.query_one("#singletons").display = False
        self.query_one("#grid-label").display = False
        self.query_one("#grid").display = False
        self.query_one("#services").display = False
        self.query_one("#status-bar").display = False

        for scope in ActionScope:
            for action in self._plugin_registry.actions_for_scope(scope):
                self._bindings.bind(action.key, f"plugin_{action.name}", action.description)

        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    @work(thread=True)
    def _refresh_data(self) -> None:
        self.app.call_from_thread(self._on_refresh_start)
        worktree_repo_decorators = list(self._plugin_registry.worktree_repo_decorators)
        environment_decorators = list(self._plugin_registry.environment_decorators)
        project_repos = self._repo_factory.get_project_repos()
        environments = self._workspace_repo.get_environments(self._workspace, project_repos)
        env_worktrees_map: dict[str, FeatureEnvironmentWorktrees] = {}
        overviews: list[FeatureEnvironmentOverview] = []
        for env in environments:
            env_status = self._workspace_svc.get_environment_status(env, project_repos, environment_decorators or None)
            env_worktrees = self._workspace_svc.get_feature_environment_worktrees(env, project_repos)
            env_worktrees_map[env.name] = env_worktrees
            repo_statuses = self._workspace_svc.get_worktree_repo_statuses(env_worktrees, worktree_repo_decorators or None)
            overviews.append(FeatureEnvironmentOverview(status=env_status, repo_statuses=repo_statuses))
        singleton_statuses = [
            self._repo_repo.get_standalone_status(r)
            for r in [*self._repo_factory.get_singleton_repos(), *self._repo_factory.get_standalone_repos()]
        ]
        self.app.call_from_thread(self._update_widgets, env_worktrees_map, overviews, singleton_statuses)

    def _on_refresh_start(self) -> None:
        try:
            self.query_one("#refresh-status", RefreshStatus).start_refresh()
        except Exception:
            pass

    def _update_widgets(
        self,
        env_worktrees_map: dict[str, FeatureEnvironmentWorktrees],
        overviews: list[FeatureEnvironmentOverview],
        singleton_statuses: list[StandaloneRepoStatus],
    ) -> None:
        self._env_worktrees = env_worktrees_map
        self.query_one("#loading-container").display = False
        self.query_one("#singletons-label").display = True
        self.query_one("#singletons").display = True
        self.query_one("#grid-label").display = True
        self.query_one("#grid").display = True
        self.query_one("#services").display = True
        self.query_one("#status-bar").display = True

        singletons = self.query_one("#singletons", StandaloneReposTable)
        singletons.statuses = singleton_statuses

        grid = self.query_one("#grid", FeatureWorktreesGrid)
        grid.statuses = overviews

        panel = self.query_one("#services", ServicePanel)
        panel.statuses = [o.status for o in overviews]

        self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    def action_refresh(self) -> None:
        self._refresh_data()

    def action_jump_prev(self) -> None:
        chain = self.focus_chain
        if chain:
            chain[0].focus()

    def action_jump_next(self) -> None:
        chain = self.focus_chain
        if chain:
            chain[-1].focus()

    def action_sync(self) -> None:
        grid = self.query_one("#grid", FeatureWorktreesGrid)
        name = grid.get_selected_worktree()
        if name is not None:
            self._run_sync(name)

    @work(thread=True)
    def _run_sync(self, name: str) -> None:
        env_worktrees = self._env_worktrees.get(name)
        if env_worktrees is None:
            return
        self._workspace_svc.sync_env(env_worktrees)
        self._refresh_data()

    def on_data_table_cell_selected(self, event: FeatureWorktreesGrid.CellSelected) -> None:
        grid = self.query_one("#grid", FeatureWorktreesGrid)
        name = grid.get_selected_worktree()
        if name is not None:
            app: WinterDashboardApp = self.app
            app.push_screen(app.screen_factory.worktree_detail_screen(name))


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
            grid = self.query_one("#grid", FeatureWorktreesGrid)
            wt_name = grid.get_selected_worktree()
            if wt_name is not None:
                self._execute_environment_action(action_name, wt_name)
        elif action.scope == ActionScope.feature_worktree:
            grid = self.query_one("#grid", FeatureWorktreesGrid)
            wt_name = grid.get_selected_worktree()
            repo_name = grid.get_selected_repo()
            if wt_name is not None and repo_name is not None:
                self._execute_worktree_action(action_name, wt_name, repo_name)

    @work(thread=True)
    def _execute_workspace_action(self, action_name: str) -> None:
        ctx = WorkspaceContext(workspace=self._workspace, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.workspace):
            if action.name == action_name:
                action.handler(ctx)
                return

    @work(thread=True)
    def _execute_environment_action(self, action_name: str, wt_name: str) -> None:
        env_worktrees = self._env_worktrees.get(wt_name)
        if env_worktrees is None:
            return
        ctx = FeatureEnvironmentContext(environment=env_worktrees.environment, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.feature_environment):
            if action.name == action_name:
                action.handler(ctx)
                return

    @work(thread=True)
    def _execute_worktree_action(self, action_name: str, wt_name: str, repo_name: str) -> None:
        env_worktrees = self._env_worktrees.get(wt_name)
        if env_worktrees is None:
            return
        wt = next(wt for wt in env_worktrees.worktrees if wt.repository.name == repo_name)
        ctx = FeatureWorktreeContext(worktree=wt, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.feature_worktree):
            if action.name == action_name:
                action.handler(ctx)
                return

    def __getattr__(self, name: str):
        if name.startswith("action_plugin_"):
            action_name = name[len("action_plugin_"):]
            def handler() -> None:
                self._run_plugin_action(action_name)
            return handler
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

