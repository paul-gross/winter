from __future__ import annotations

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.tui.widgets.refresh_status import RefreshStatus

from winter_cli.modules.workspace.models import (
    FeatureEnvironmentStatus,
    RepoError,
    RepoStatus,
    Workspace,
    WorktreeRepoStatus,
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


class WorktreeDetailScreen(Screen):

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("s", "sync", "Sync"),
        Binding("L", "open_log", "Log"),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "cursor_right", "Right", show=False),
        Binding("q", "back", "Back"),
    ]

    def __init__(
        self,
        worktree_name: str,
        workspace_svc: WorkspaceService,
        workspace_repo: IReadWorkspaceRepository,
        repo_repo: IReadRepoRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
        plugin_registry: PluginRegistry,
        error_log: ErrorLogService,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.worktree_name = worktree_name
        self._workspace_svc = workspace_svc
        self._workspace_repo = workspace_repo
        self._repo_repo = repo_repo
        self._repo_factory = repo_factory
        self._workspace = workspace
        self._plugin_registry = plugin_registry
        self._error_log = error_log
        self._env_status: FeatureEnvironmentStatus | None = None
        self._repo_statuses: list[WorktreeRepoStatus] = []
        self._focused_repo: str | None = None
        self._repo_detail: RepoStatus | None = None
        self._detail_repo_keys: list[str] = []

    def compose(self):
        yield Header()
        with Horizontal(id="detail-title-bar"):
            yield Static(id="detail-header")
            yield RefreshStatus(id="refresh-status")
        yield DataTable(id="detail-repos")
        with Horizontal(id="detail-bottom"):
            yield Vertical(Static(id="repo-info"), id="detail-info")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#detail-repos", DataTable)
        table.cursor_type = "row"

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
        try:
            project_repos = self._repo_factory.get_project_repos()
            env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
            env_status = self._workspace_svc.get_environment_status(env, project_repos, environment_decorators or None)
            env_worktrees = self._workspace_svc.get_feature_environment_worktrees(env, project_repos)
        except RepoError as exc:
            self._capture_error(f"WorktreeDetailScreen({self.worktree_name}).refresh", exc)
            self.app.call_from_thread(self._on_refresh_finished)
            return

        def _on_repo_error(wt, exc):
            self._capture_error(
                f"WorktreeDetailScreen({self.worktree_name}).refresh({wt.repository.name})", exc,
            )
        repo_statuses = self._workspace_svc.get_worktree_repo_statuses(
            env_worktrees,
            worktree_repo_decorators or None,
            on_repo_error=_on_repo_error,
        )
        self.app.call_from_thread(self._update_widgets, env_status, repo_statuses)

    def _capture_error(self, location: str, exc: RepoError) -> None:
        """Log a RepoError to the session log and toast (deduped) without crashing."""
        entry, should_notify = self._error_log.record(location=location, exc=exc)
        if should_notify:
            self.app.call_from_thread(
                self.app.notify,
                f"{entry.message}\nPress L for log",
                title="git error",
                severity="error",
                timeout=6,
            )

    def _on_refresh_finished(self) -> None:
        try:
            self.query_one("#refresh-status", RefreshStatus).finish_refresh()
        except Exception:
            pass

    def action_open_log(self) -> None:
        from winter_cli.modules.tui.app import WinterDashboardApp
        app: WinterDashboardApp = self.app
        app.push_screen(app.screen_factory.error_log_screen())

    def _on_refresh_start(self) -> None:
        try:
            self.query_one("#refresh-status", RefreshStatus).start_refresh()
        except Exception:
            pass

    def _update_widgets(
        self,
        env_status: FeatureEnvironmentStatus,
        repo_statuses: list[WorktreeRepoStatus],
    ) -> None:
        self._env_status = env_status
        self._repo_statuses = repo_statuses

        header = self.query_one("#detail-header", Static)
        badges = " ".join(v for v in env_status.extensions.values() if v)
        branch_text = env_status.feature_branch or "disconnected"
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
            table.add_column("Ahead", key="ahead")
            table.add_column("Behind", key="behind")
            table.add_column("Dirty", key="dirty")
            table.add_column("", key="ext")
            self._detail_repo_keys = repo_keys

            for rs in repo_statuses:
                name = rs.worktree.repository.name
                table.add_row(
                    name,
                    rs.branch or "—",
                    *self._render_repo_cells(rs),
                    self._render_extensions(rs),
                    key=name,
                )
        else:
            for rs in repo_statuses:
                name = rs.worktree.repository.name
                table.update_cell(name, "branch", rs.branch or "—")
                ahead, behind, dirty = self._render_repo_cells(rs)
                table.update_cell(name, "ahead", ahead)
                table.update_cell(name, "behind", behind)
                table.update_cell(name, "dirty", dirty)
                table.update_cell(name, "ext", self._render_extensions(rs), update_width=True)
            table._update_count += 1
            table.refresh()

        if len(repo_statuses) > 0 and self._focused_repo is None:
            self._focused_repo = repo_statuses[0].worktree.repository.name

        if self._focused_repo is not None:
            self._load_repo_detail(self._focused_repo)

        self.query_one("#refresh-status", RefreshStatus).finish_refresh()

    @staticmethod
    def _render_repo_cells(rs: WorktreeRepoStatus) -> tuple[Text, Text, Text]:
        ahead = Text(str(rs.ahead), style="green") if rs.ahead > 0 else Text("0", style="dim")
        behind = Text(str(rs.behind), style="yellow") if rs.behind > 0 else Text("0", style="dim")
        dirty = Text(str(rs.dirty_count), style="red") if rs.dirty_count > 0 else Text("0", style="dim")
        return ahead, behind, dirty

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
            env_worktrees = self._workspace_svc.get_feature_environment_worktrees(env, project_repos)
            wt = next(wt for wt in env_worktrees.worktrees if wt.repository.name == repo_name)
            detail = self._repo_repo.get_worktree_status(wt)
        except RepoError as exc:
            self._capture_error(
                f"WorktreeDetailScreen({self.worktree_name}).load_repo_detail({repo_name})", exc,
            )
            return
        self.app.call_from_thread(self._update_repo_info, detail)

    def _update_repo_info(self, detail: RepoStatus) -> None:
        self._repo_detail = detail
        info = self.query_one("#repo-info", Static)

        lines = [
            f"[bold]{detail.name}[/bold]",
            f"Branch:   {detail.branch or '—'}",
            f"Tracking: {detail.tracking_branch or '—'}",
            f"Ahead:    {detail.ahead}  Behind: {detail.behind}",
        ]

        if len(detail.dirty_files) > 0:
            lines.append(f"\n[bold]Modified ({len(detail.dirty_files)}):[/bold]")
            for f in detail.dirty_files[:15]:
                lines.append(f"  {f}")
            remaining = len(detail.dirty_files) - 15
            if remaining > 0:
                lines.append(f"  ... and {remaining} more")

        if len(detail.recent_commits) > 0:
            lines.append("\n[bold]Recent commits:[/bold]")
            for c in detail.recent_commits[:10]:
                lines.append(f"  [dim]{c.short_hash}[/dim] {c.message}")

        info.update("\n".join(lines))

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

    def action_sync(self) -> None:
        self._run_sync()

    @work(thread=True)
    def _run_sync(self) -> None:
        try:
            project_repos = self._repo_factory.get_project_repos()
            env = self._workspace_repo.get_environment(self._workspace, self.worktree_name)
            env_worktrees = self._workspace_svc.get_feature_environment_worktrees(env, project_repos)
            self._workspace_svc.sync_env(env_worktrees)
        except RepoError as exc:
            self._capture_error(f"WorktreeDetailScreen({self.worktree_name}).sync", exc)
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
        elif action.scope == ActionScope.feature_worktree:
            if self._focused_repo is not None:
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
        env_worktrees = self._workspace_svc.get_feature_environment_worktrees(env, project_repos)
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

    def action_back(self) -> None:
        self.app.pop_screen()
