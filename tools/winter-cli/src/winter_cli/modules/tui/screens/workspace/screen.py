from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

from textual import work
from textual.containers import Center, Horizontal, Middle
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, LoadingIndicator, Static

from winter_cli.config.models import DashboardLayout
from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.tui.keybindings import (
    KeybindingMixin,
    KeybindingResolver,
    all_builtin_action_ids,
    plugin_action_bindings,
)
from winter_cli.modules.tui.keybindings.actions import PLUGIN_ID_PREFIX, WORKSPACE_ACTIONS
from winter_cli.modules.tui.screens.plugin_action_mixin import PluginActionMixin
from winter_cli.modules.tui.screens.workspace.feature_worktrees import FeatureWorktreesGrid
from winter_cli.modules.tui.screens.workspace.standalone_repos import StandaloneReposTable
from winter_cli.modules.tui.widgets.refresh_status import RefreshStatus
from winter_cli.modules.tui.widgets.service_panel import ServicePanel
from winter_cli.modules.workspace.models import (
    FeatureEnvironmentOverview,
    FeatureEnvironmentWorktrees,
    RepoError,
    StandaloneRepoStatus,
    Workspace,
    WorktreeRepoStatus,
)
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService
from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import (
    ActionInvocation,
    ActionScope,
    FeatureEnvironmentContext,
    FeatureWorktreeContext,
    StandaloneRepoContext,
    TuiAction,
    WorkspaceContext,
)

if TYPE_CHECKING:
    from winter_cli.modules.tui.app import WinterDashboardApp


class WorkspaceScreen(KeybindingMixin, PluginActionMixin, Screen):
    # Bindings are installed in on_mount from config-resolved action ids
    # (see winter_cli.modules.tui.keybindings.actions.WORKSPACE_ACTIONS), not
    # hardcoded here, so users can remap them via `[keybindings]`.

    def __init__(
        self,
        snapshot_svc: WorkspaceSnapshotService,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        plugin_registry: PluginRegistry,
        error_log: ErrorLogService,
        keybinding_resolver: KeybindingResolver,
        dashboard_layout: DashboardLayout = DashboardLayout.auto,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._snapshot_svc = snapshot_svc
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._plugin_registry = plugin_registry
        self._error_log = error_log
        self._keybinding_resolver = keybinding_resolver
        self._dashboard_layout = dashboard_layout
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
        yield FeatureWorktreesGrid(layout=self._dashboard_layout, id="grid")
        with Horizontal(id="status-bar"):
            yield Static(
                "[green]+N[/green] [dim]ahead of main[/dim]  "
                "[yellow]-N[/yellow] [dim]behind main[/dim]  "
                "[red]N files[/red] [dim]uncommitted[/dim]  "
                "[cyan]\\[+N, -N][/cyan] [dim]ahead/behind tracking[/dim]  "
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

        self._install_dashboard_keybindings()

        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _install_dashboard_keybindings(self) -> None:
        """Resolve built-in + plugin action keys from config and install them.

        The workspace screen owns the global unknown-action-id check: it is the
        only screen that sees every scope's plugin actions, so it validates the
        whole `[keybindings]` table against the union of known ids here.
        """
        plugin_bindings = plugin_action_bindings(self._plugin_registry, tuple(ActionScope))
        actions = [*WORKSPACE_ACTIONS, *plugin_bindings]
        errors = self._install_keybindings(actions)

        known_ids = all_builtin_action_ids() | {
            f"{PLUGIN_ID_PREFIX}{a.name}" for a in self._plugin_registry.tui_actions
        }
        errors += self._keybinding_resolver.unknown_id_errors(known_ids)

        for message in errors:
            self.app.notify(message, title="keybindings", severity="error", timeout=8)

    @work(thread=True)
    def _refresh_data(self) -> None:
        """Read every env and standalone repo, isolating failures per-source.

        One broken repo would otherwise poison the entire refresh: the worker
        bails on the first RepoError, _update_widgets never runs, and the
        dashboard stays stuck on the loading splash. Catching at the env /
        standalone-repo boundary keeps the rest of the dashboard responsive
        while every individual failure still lands in the Log tab.

        All git-probing is delegated to `WorkspaceSnapshotService.collect_for_dashboard()`
        so the dashboard and `ws status` cannot diverge on what they read.
        """
        self.app.call_from_thread(self._on_refresh_start)
        worktree_repo_decorators = list(self._plugin_registry.worktree_repo_decorators)
        environment_decorators = list(self._plugin_registry.environment_decorators)

        def _on_repo_error(wt, exc):
            self._capture_error(f"WorkspaceScreen.refresh({wt.repository.name})", exc)

        try:
            data = self._snapshot_svc.collect_for_dashboard(
                on_repo_error=_on_repo_error,
                env_decorators=environment_decorators or None,
                worktree_repo_decorators=worktree_repo_decorators or None,
            )
        except RepoError as exc:
            self._capture_error("WorkspaceScreen.refresh", exc)
            self.app.call_from_thread(self._update_widgets, {}, [], [])
            return

        # Reconstruct FeatureEnvironmentWorktrees from the overviews for the
        # plugin action execution methods — no extra git probing needed.
        env_worktrees_map: dict[str, FeatureEnvironmentWorktrees] = {
            overview.status.environment.name: FeatureEnvironmentWorktrees(
                environment=overview.status.environment,
                worktrees=[rs.worktree for rs in overview.repo_statuses],
            )
            for overview in data.overviews
        }

        self.app.call_from_thread(
            self._update_widgets,
            env_worktrees_map,
            data.overviews,
            data.standalone_statuses,
            data.main_statuses,
        )

    def action_open_log(self) -> None:
        app = cast("WinterDashboardApp", self.app)
        app.push_screen(app.screen_factory.error_log_screen())

    def _on_refresh_start(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#refresh-status", RefreshStatus).start_refresh()

    def _update_widgets(
        self,
        env_worktrees_map: dict[str, FeatureEnvironmentWorktrees],
        overviews: list[FeatureEnvironmentOverview],
        singleton_statuses: list[StandaloneRepoStatus],
        main_statuses: dict[str, WorktreeRepoStatus] | None = None,
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
        # Set main_statuses without firing its watcher — the statuses watcher
        # calls _update_in_place(), which reads main_statuses, so one visual
        # pass covers both updates.
        grid.set_reactive(FeatureWorktreesGrid.main_statuses, main_statuses if main_statuses is not None else {})
        grid.statuses = overviews

        self._update_grid_label(grid)

        panel = self.query_one("#services", ServicePanel)
        panel.statuses = [o.status for o in overviews]

        self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    def action_refresh(self) -> None:
        self._refresh_data()

    def _update_grid_label(self, grid: FeatureWorktreesGrid) -> None:
        self.query_one("#grid-label", Static).update(
            f"[bold]Feature Repositories — {grid.active_layout_label()}[/bold]"
        )

    def action_cycle_layout(self) -> None:
        grid = self.query_one("#grid", FeatureWorktreesGrid)
        grid.cycle_layout()
        self._update_grid_label(grid)

    def action_jump_prev(self) -> None:
        chain = self.focus_chain
        if chain:
            chain[0].focus()

    def action_jump_next(self) -> None:
        chain = self.focus_chain
        if chain:
            chain[-1].focus()

    def on_data_table_cell_selected(self, event: FeatureWorktreesGrid.CellSelected) -> None:
        self._open_feature_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # The feature grid is cursor_type="cell" (CellSelected, above); only the
        # row-cursor standalone table raises RowSelected on this screen. Enter on
        # a standalone row drills into its single-repo detail view.
        if event.data_table.id != "singletons":
            return
        self._open_standalone_detail()

    def action_open_detail(self) -> None:
        """Drill into the focused row's detail view (the `worktree.open_detail` id).

        Routes to whichever table holds focus, so a remapped key behaves like
        Enter does on either the feature grid or the standalone table.
        """
        focused = self.focused
        if focused is not None and focused.id == "singletons":
            self._open_standalone_detail()
        else:
            self._open_feature_detail()

    def _open_feature_detail(self) -> None:
        grid = self.query_one("#grid", FeatureWorktreesGrid)
        name = grid.get_selected_worktree()
        if name is not None:
            repo_name = grid.get_selected_repo()
            app = cast("WinterDashboardApp", self.app)
            app.push_screen(app.screen_factory.worktree_detail_screen(name, focused_repo=repo_name))

    def _open_standalone_detail(self) -> None:
        repo_name = self.query_one("#singletons", StandaloneReposTable).get_selected_repo()
        if repo_name is not None:
            app = cast("WinterDashboardApp", self.app)
            app.push_screen(app.screen_factory.standalone_detail_screen(repo_name))

    def _resolve_action_scope(self, action: TuiAction) -> ActionScope | None:
        """Resolve the originating scope for a plugin action, biased by focused area.

        Uses a deterministic priority order based on which area holds focus:
        - Singletons focused: standalone_repository first, then workspace fallback.
        - Grid focused (default): feature_worktree first, then feature_environment,
          then workspace, then standalone_repository.

        Returns the first scope in the ordered list that is present in the
        action's declared scopes, or None if none match.
        """
        focused = self.focused
        on_standalone = focused is not None and focused.id == "singletons"
        if on_standalone:
            order = [
                ActionScope.standalone_repository,
                ActionScope.workspace,
                ActionScope.feature_worktree,
                ActionScope.feature_environment,
            ]
        else:
            order = [
                ActionScope.feature_worktree,
                ActionScope.feature_environment,
                ActionScope.workspace,
                ActionScope.standalone_repository,
            ]
        for scope in order:
            if scope in action.scopes:
                return scope
        return None

    def _run_plugin_action(self, action_name: str) -> None:
        action = next(
            (a for a in self._plugin_registry.tui_actions if a.name == action_name),
            None,
        )
        if action is None:
            return

        originating_scope = self._resolve_action_scope(action)
        if originating_scope is None:
            return

        if originating_scope == ActionScope.workspace:
            self._execute_workspace_action(action_name, originating_scope)
        elif originating_scope == ActionScope.feature_environment:
            grid = self.query_one("#grid", FeatureWorktreesGrid)
            wt_name = grid.get_selected_worktree()
            if wt_name is not None:
                self._execute_environment_action(action_name, wt_name, originating_scope)
        elif originating_scope == ActionScope.feature_worktree:
            grid = self.query_one("#grid", FeatureWorktreesGrid)
            wt_name = grid.get_selected_worktree()
            repo_name = grid.get_selected_repo()
            if wt_name is not None and repo_name is not None:
                self._execute_worktree_action(action_name, wt_name, repo_name, originating_scope)
        elif originating_scope == ActionScope.standalone_repository:
            singletons = self.query_one("#singletons", StandaloneReposTable)
            repo_name = singletons.get_selected_repo()
            if repo_name is not None:
                self._execute_standalone_action(action_name, repo_name, originating_scope)

    @work(thread=True)
    def _execute_workspace_action(self, action_name: str, originating_scope: ActionScope) -> None:
        ctx = WorkspaceContext(workspace=self._workspace, suspend=self.app.suspend)
        inv = ActionInvocation(scope=originating_scope, context=ctx)
        for action in self._plugin_registry.actions_for_scope(originating_scope):
            if action.name == action_name:
                action.handler(inv)
                return

    @work(thread=True)
    def _execute_environment_action(self, action_name: str, wt_name: str, originating_scope: ActionScope) -> None:
        env_worktrees = self._env_worktrees.get(wt_name)
        if env_worktrees is None:
            return
        ctx = FeatureEnvironmentContext(environment=env_worktrees.environment, suspend=self.app.suspend)
        inv = ActionInvocation(scope=originating_scope, context=ctx)
        for action in self._plugin_registry.actions_for_scope(originating_scope):
            if action.name == action_name:
                action.handler(inv)
                return

    @work(thread=True)
    def _execute_worktree_action(
        self, action_name: str, wt_name: str, repo_name: str, originating_scope: ActionScope
    ) -> None:
        env_worktrees = self._env_worktrees.get(wt_name)
        if env_worktrees is None:
            return
        wt = next((wt for wt in env_worktrees.worktrees if wt.repository.name == repo_name), None)
        if wt is None:
            return
        ctx = FeatureWorktreeContext(worktree=wt, suspend=self.app.suspend)
        inv = ActionInvocation(scope=originating_scope, context=ctx)
        for action in self._plugin_registry.actions_for_scope(originating_scope):
            if action.name == action_name:
                action.handler(inv)
                return

    @work(thread=True)
    def _execute_standalone_action(self, action_name: str, repo_name: str, originating_scope: ActionScope) -> None:
        repo = self._repo_factory.find_standalone(repo_name)
        if repo is None:
            return
        ctx = StandaloneRepoContext(repo=repo, suspend=self.app.suspend)
        inv = ActionInvocation(scope=originating_scope, context=ctx)
        for action in self._plugin_registry.actions_for_scope(originating_scope):
            if action.name == action_name:
                action.handler(inv)
                return
