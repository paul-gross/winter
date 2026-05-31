from __future__ import annotations

import contextlib
from typing import cast

from textual import work
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.tui.keybindings import KeybindingMixin, KeybindingResolver, plugin_action_bindings
from winter_cli.modules.tui.keybindings.actions import STANDALONE_DETAIL_ACTIONS
from winter_cli.modules.tui.screens.plugin_action_mixin import PluginActionMixin
from winter_cli.modules.tui.widgets.refresh_status import RefreshStatus
from winter_cli.modules.tui.widgets.repo_detail_view import PanelOutcome, RepoDetailView, render_detail_panels
from winter_cli.modules.workspace.models import (
    RepoError,
    RepoStatus,
    StandaloneRepository,
    Workspace,
)
from winter_cli.modules.workspace.repo_repository import IReadRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.plugins.loader import PluginRegistry
from winter_cli.plugins.types import ActionScope, DetailPanelContext, StandaloneRepoContext, WorkspaceContext


class StandaloneDetailScreen(KeybindingMixin, PluginActionMixin, Screen):
    """Detail view for one standalone repository.

    The single-repo subset of `WorktreeDetailScreen`: no multi-repo table (a
    standalone is one repo), just the shared `RepoDetailView` body — branch,
    tracking status, dirty files, recent commits, and any contributed
    `IDetailPanel` tabs. Reached by pressing Enter on a standalone row.

    Bindings are installed in on_mount from config-resolved action ids
    (keybindings.actions.STANDALONE_DETAIL_ACTIONS), not hardcoded here.
    """

    def __init__(
        self,
        repo_name: str,
        repo_repo: IReadRepoRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        plugin_registry: PluginRegistry,
        error_log: ErrorLogService,
        keybinding_resolver: KeybindingResolver,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.repo_name = repo_name
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._plugin_registry = plugin_registry
        self._error_log = error_log
        self._keybinding_resolver = keybinding_resolver
        self._detail_panels = list(plugin_registry.detail_panels)
        # Retained for parity with WorktreeDetailScreen and as a test/observability
        # hook on the last-rendered status; not read by the screen itself.
        self._repo_detail: RepoStatus | None = None

    def compose(self):
        yield Header()
        with Horizontal(id="detail-title-bar"):
            yield Static(id="detail-header")
            yield RefreshStatus(id="refresh-status")
        yield RepoDetailView(self._detail_panels, id="detail-info")
        yield Footer()

    def on_mount(self) -> None:
        # Only workspace- and standalone-scoped actions have a resolvable
        # context here; feature-env / feature-worktree actions don't apply to a
        # standalone repo, so we don't advertise their keys.
        plugin_bindings = plugin_action_bindings(
            self._plugin_registry,
            (ActionScope.workspace, ActionScope.standalone_repository),
        )
        for message in self._install_keybindings([*STANDALONE_DETAIL_ACTIONS, *plugin_bindings]):
            self.app.notify(message, title="keybindings", severity="error", timeout=8)

        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _resolve_repo(self) -> StandaloneRepository | None:
        return self._repo_factory.find_standalone(self.repo_name)

    @work(thread=True)
    def _refresh_data(self) -> None:
        self.app.call_from_thread(self._on_refresh_start)
        repo = self._resolve_repo()
        if repo is None:
            self.app.call_from_thread(self._on_refresh_finished)
            return
        try:
            detail = self._repo_repo.get_standalone_detail(repo)
        except RepoError as exc:
            self._capture_error(f"StandaloneDetailScreen({self.repo_name}).refresh", exc)
            self.app.call_from_thread(self._on_refresh_finished)
            return
        outcomes = render_detail_panels(self._detail_panels, DetailPanelContext(repo=repo))
        self.app.call_from_thread(self._update_widgets, detail, outcomes)

    def _update_widgets(self, detail: RepoStatus, outcomes: list[PanelOutcome]) -> None:
        self._repo_detail = detail

        header = self.query_one("#detail-header", Static)
        branch = detail.branch or "detached"
        tracking = detail.tracking_branch or "no upstream"
        header.update(f"  [bold]{detail.name}[/bold]  {branch}  [dim]→ {tracking}[/dim]")

        self.query_one("#detail-info", RepoDetailView).show_repo(detail, outcomes)
        self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    def _on_refresh_start(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#refresh-status", RefreshStatus).start_refresh()

    def _on_refresh_finished(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    def action_refresh(self) -> None:
        self._refresh_data()

    def action_open_log(self) -> None:
        from winter_cli.modules.tui.app import WinterDashboardApp

        app = cast(WinterDashboardApp, self.app)
        app.push_screen(app.screen_factory.error_log_screen())

    def action_back(self) -> None:
        self.app.pop_screen()

    def _run_plugin_action(self, action_name: str) -> None:
        action = next(
            (a for a in self._plugin_registry.tui_actions if a.name == action_name),
            None,
        )
        if action is None:
            return
        if action.scope == ActionScope.workspace:
            self._execute_workspace_action(action_name)
        elif action.scope == ActionScope.standalone_repository:
            self._execute_standalone_action(action_name)

    @work(thread=True)
    def _execute_workspace_action(self, action_name: str) -> None:
        ctx = WorkspaceContext(workspace=self._workspace, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.workspace):
            if action.name == action_name:
                action.handler(ctx)
                return

    @work(thread=True)
    def _execute_standalone_action(self, action_name: str) -> None:
        repo = self._resolve_repo()
        if repo is None:
            return
        ctx = StandaloneRepoContext(repo=repo, suspend=self.app.suspend)
        for action in self._plugin_registry.actions_for_scope(ActionScope.standalone_repository):
            if action.name == action_name:
                action.handler(ctx)
                return
