from __future__ import annotations

import contextlib
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.widgets import DataTable
from textual.widgets.data_table import ColumnKey

from winter_cli.config.models import DashboardLayout
from winter_cli.modules.tui.screens.workspace.repo_status import render_repo_cell
from winter_cli.modules.workspace.models import FeatureEnvironmentOverview, WorktreeRepoStatus

# Pushpin marks pinned project repos in the row label. The trailing
# U+FE0E (variation selector-15) requests text-style monochrome rendering
# instead of the colored emoji presentation. Most modern terminals render
# it as 2 columns wide; unpinned repos pad with two spaces so all repo
# names align on the same column regardless of pinned status.
_PIN_GLYPH = "📌︎"
_PIN_PAD = "  "


class FeatureWorktreesGrid(DataTable):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "cursor_left", "Left", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]

    statuses: reactive[list[FeatureEnvironmentOverview]] = reactive(list, always_update=True)
    main_statuses: reactive[dict[str, WorktreeRepoStatus]] = reactive(dict, always_update=True)

    def __init__(self, layout: DashboardLayout = DashboardLayout.auto, **kwargs) -> None:
        super().__init__(**kwargs)
        self._configured_layout = layout
        self._env_keys: list[str] = []
        self._repo_keys: list[str] = []
        self._pinned_by_name: dict[str, bool] = {}
        # For list layout: list of (env_name, repo_name) in row order.
        self._list_rows: list[tuple[str, str]] = []

    def on_mount(self) -> None:
        self.cursor_type = "cell"
        self.header_height = 2

    def action_cursor_left(self) -> None:
        row, col = self.cursor_coordinate
        self.move_cursor(row=row, column=max(0, col - 1), animate=False)

    def action_cursor_down(self) -> None:
        row, col = self.cursor_coordinate
        self.move_cursor(row=row + 1, column=col, animate=False)

    def action_cursor_up(self) -> None:
        row, col = self.cursor_coordinate
        if row <= 0:
            with contextlib.suppress(Exception):
                self.screen.query_one("#singletons").focus()
            return
        self.move_cursor(row=row - 1, column=col, animate=False)

    def action_cursor_right(self) -> None:
        row, col = self.cursor_coordinate
        self.move_cursor(row=row, column=col + 1, animate=False)

    @staticmethod
    def _resolve_auto(n_repos: int, n_envs: int) -> DashboardLayout:
        """Resolve DashboardLayout.auto to a concrete layout given data counts."""
        if n_repos == 1:
            return DashboardLayout.list
        if n_repos > n_envs:
            return DashboardLayout.repos_as_rows
        return DashboardLayout.repos_as_columns

    def _active_layout(self) -> DashboardLayout:
        """Return the concrete layout to render, resolving auto if needed."""
        if self._configured_layout is not DashboardLayout.auto:
            return self._configured_layout
        if not self.statuses:
            return DashboardLayout.repos_as_rows
        n_repos = len(self.statuses[0].repo_statuses)
        n_envs = len(self.statuses)
        return self._resolve_auto(n_repos, n_envs)

    def active_layout_label(self) -> str:
        """Return a display string for the current layout, e.g. 'auto→list' or 'list'."""
        resolved = self._active_layout()
        if self._configured_layout is DashboardLayout.auto:
            return f"auto→{resolved.value}"
        return resolved.value

    def _structure_matches(self) -> bool:
        """Check if repos-as-rows structure is still valid (fast-path refresh)."""
        env_keys = [o.status.environment.name for o in self.statuses]
        repo_keys = [rs.worktree.repository.name for rs in self.statuses[0].repo_statuses] if self.statuses else []
        return env_keys == self._env_keys and repo_keys == self._repo_keys

    @staticmethod
    def _env_badges(overview: FeatureEnvironmentOverview) -> str:
        """Env-scoped extension badge string (e.g. service status), space-joined for display."""
        return " ".join(v for v in overview.status.extensions.values() if v)

    @staticmethod
    def _column_header(overview: FeatureEnvironmentOverview) -> Text:
        badges = FeatureWorktreesGrid._env_badges(overview)
        branch = overview.status.feature_branch_label(disconnected="—")
        name = overview.status.environment.name.capitalize()
        title = f"{name} {badges}".rstrip()
        return Text(f"{title:<12}\n{branch}")

    def watch_statuses(self) -> None:
        if len(self.statuses) == 0:
            self.clear(columns=True)
            self._env_keys = []
            self._repo_keys = []
            self._list_rows = []
            return

        layout = self._active_layout()

        if layout is DashboardLayout.repos_as_rows and self._structure_matches():
            self._update_in_place()
            return

        coord = self.cursor_coordinate
        if layout is DashboardLayout.repos_as_rows:
            self._full_rebuild_rows()
        elif layout is DashboardLayout.repos_as_columns:
            self._full_rebuild_cols()
        elif layout is DashboardLayout.list:
            self._full_rebuild_list()
        with contextlib.suppress(Exception):
            self.move_cursor(row=coord.row, column=coord.column, animate=False)

    def _full_rebuild_rows(self) -> None:
        """repos-as-rows: col 0 = repo label, one column per env."""
        self.clear(columns=True)

        self.add_column(f"{'Repositories':<40}", key="repo")
        for overview in self.statuses:
            self.add_column(self._column_header(overview), key=overview.status.environment.name)

        self._env_keys = [o.status.environment.name for o in self.statuses]
        self._repo_keys = []
        self._list_rows = []

        repo_lookup = self._build_repo_lookup()
        first_repo_statuses = self.statuses[0].repo_statuses if self.statuses else []
        repo_names = [rs.worktree.repository.name for rs in first_repo_statuses]
        self._pinned_by_name = {
            rs.worktree.repository.name: rs.worktree.repository.pinned for rs in first_repo_statuses
        }
        self._repo_keys = list(repo_names)

        for repo_name in repo_names:
            prefix = f"{_PIN_GLYPH} " if self._pinned_by_name.get(repo_name) else f"{_PIN_PAD} "
            label = self._build_repo_label(prefix, repo_name)
            row_cells: list = [label]
            for overview in self.statuses:
                repo_status = repo_lookup[overview.status.environment.name].get(repo_name)
                row_cells.append(render_repo_cell(repo_status) if repo_status else Text("-"))
            self.add_row(*row_cells, key=repo_name)

    def _update_in_place(self) -> None:
        repo_lookup = self._build_repo_lookup()

        for overview in self.statuses:
            col_key = ColumnKey(overview.status.environment.name)
            self.columns[col_key].label = self._column_header(overview)

        for repo_name in self._repo_keys:
            prefix = f"{_PIN_GLYPH} " if self._pinned_by_name.get(repo_name) else f"{_PIN_PAD} "
            label = self._build_repo_label(prefix, repo_name)
            self.update_cell(repo_name, "repo", label, update_width=True)
            for overview in self.statuses:
                col_key = overview.status.environment.name
                repo_status = repo_lookup[col_key].get(repo_name)
                value = render_repo_cell(repo_status) if repo_status else Text("-")
                self.update_cell(repo_name, col_key, value, update_width=True)

    def _full_rebuild_cols(self) -> None:
        """repos-as-columns: col 0 = env label, one column per repo, rows = envs."""
        self.clear(columns=True)

        # Gather repo names from first env (all envs should have same repos)
        first_repo_statuses = self.statuses[0].repo_statuses if self.statuses else []
        repo_names = [rs.worktree.repository.name for rs in first_repo_statuses]
        self._pinned_by_name = {
            rs.worktree.repository.name: rs.worktree.repository.pinned for rs in first_repo_statuses
        }

        self._env_keys = [o.status.environment.name for o in self.statuses]
        self._repo_keys = list(repo_names)
        self._list_rows = []

        # Col 0: env label header
        self.add_column(f"{'Environment':<20}", key="_env")
        for repo_name in repo_names:
            pin = f"{_PIN_GLYPH} " if self._pinned_by_name.get(repo_name) else f"{_PIN_PAD} "
            self.add_column(f"{pin}{repo_name}", key=repo_name)

        repo_lookup = self._build_repo_lookup()
        for overview in self.statuses:
            env_name = overview.status.environment.name
            env_label = self._column_header(overview)
            row_cells: list = [env_label]
            for repo_name in repo_names:
                repo_status = repo_lookup[env_name].get(repo_name)
                row_cells.append(render_repo_cell(repo_status) if repo_status else Text("-"))
            self.add_row(*row_cells, key=env_name)

    def _full_rebuild_list(self) -> None:
        """list: one row per (env, repo) with env/project/remote/git-status/service-status columns."""
        self.clear(columns=True)

        self._env_keys = [o.status.environment.name for o in self.statuses]
        first_repo_statuses = self.statuses[0].repo_statuses if self.statuses else []
        repo_names = [rs.worktree.repository.name for rs in first_repo_statuses]
        self._pinned_by_name = {
            rs.worktree.repository.name: rs.worktree.repository.pinned for rs in first_repo_statuses
        }
        self._repo_keys = list(repo_names)
        self._list_rows = []

        self.add_column(f"{'Env':<12}", key="_env")
        self.add_column(f"{'Project':<20}", key="_project")
        self.add_column(f"{'Remote':<30}", key="_remote")
        self.add_column(f"{'Git status':<20}", key="_git")
        self.add_column(f"{'Services':<20}", key="_services")

        repo_lookup = self._build_repo_lookup()

        row_idx = 0
        for overview in self.statuses:
            env_name = overview.status.environment.name
            # Env and service status are env-scoped → shown once per env section.
            # Remote is per-repo (each worktree tracks its own upstream) → shown on every row.
            service_badges = self._env_badges(overview)
            first_in_env = True
            for repo_name in repo_names:
                repo_status = repo_lookup[env_name].get(repo_name)
                git_cell = render_repo_cell(repo_status) if repo_status else Text("-")
                pin = f"{_PIN_GLYPH} " if self._pinned_by_name.get(repo_name) else f"{_PIN_PAD} "
                project_cell = Text(f"{pin}{repo_name}")
                remote = repo_status.tracking_branch if repo_status and repo_status.tracking_branch else "—"
                remote_cell = Text(remote)

                if first_in_env:
                    # capitalize() matches the repos-as-rows/columns env-header style
                    # (other layouts use _column_header, which also capitalizes).
                    env_cell = Text(env_name.capitalize())
                    svc_cell = Text(service_badges)
                    first_in_env = False
                else:
                    env_cell = Text("")
                    svc_cell = Text("")

                row_key = f"_list_{row_idx}"
                self.add_row(env_cell, project_cell, remote_cell, git_cell, svc_cell, key=row_key)
                self._list_rows.append((env_name, repo_name))
                row_idx += 1

    def _build_repo_label(self, prefix: str, repo_name: str) -> Text:
        label = Text(f"{prefix}{repo_name}")
        ms = self.main_statuses.get(repo_name)
        if ms is not None:
            label.append(" ")
            label.append_text(render_repo_cell(ms, include_extensions=False))
        return label

    _CYCLE_ORDER: ClassVar[tuple[DashboardLayout, ...]] = (
        DashboardLayout.auto,
        DashboardLayout.repos_as_columns,
        DashboardLayout.repos_as_rows,
        DashboardLayout.list,
    )

    def cycle_layout(self) -> None:
        """Advance the active layout through the cycle order and re-render."""
        try:
            idx = self._CYCLE_ORDER.index(self._configured_layout)
        except ValueError:
            idx = 0
        self._configured_layout = self._CYCLE_ORDER[(idx + 1) % len(self._CYCLE_ORDER)]
        # Invalidate the structure cache so _structure_matches() forces a full
        # rebuild rather than the in-place fast-path after a layout switch.
        self._env_keys = []
        self._repo_keys = []
        # watch_statuses handles cursor preservation for all layout branches.
        self.watch_statuses()

    def watch_main_statuses(self) -> None:
        if not self._repo_keys:
            return
        layout = self._active_layout()
        if layout is DashboardLayout.repos_as_rows:
            self._update_in_place()
        # For other layouts, main_statuses aren't shown in the row-label column,
        # so no update needed.

    def _build_repo_lookup(self) -> dict[str, dict[str, WorktreeRepoStatus]]:
        lookup: dict[str, dict[str, WorktreeRepoStatus]] = {}
        for overview in self.statuses:
            by_name: dict[str, WorktreeRepoStatus] = {}
            for repo_status in overview.repo_statuses:
                by_name[repo_status.worktree.repository.name] = repo_status
            lookup[overview.status.environment.name] = by_name
        return lookup

    def get_selected_worktree(self) -> str | None:
        """Return the env name for the cursor cell, adapting to the active layout."""
        if self.cursor_coordinate is None:
            return None
        if not self.statuses:
            return None

        layout = self._active_layout()
        row_idx = self.cursor_coordinate.row
        col_idx = self.cursor_coordinate.column

        if layout is DashboardLayout.repos_as_rows:
            # col 0 = repo label; cols 1..N = envs
            if col_idx < 1:
                return None
            wt_idx = col_idx - 1
            if wt_idx >= len(self.statuses):
                return None
            return self.statuses[wt_idx].status.environment.name

        elif layout is DashboardLayout.repos_as_columns:
            # rows = envs (col 0 = env label)
            if row_idx < 0 or row_idx >= len(self.statuses):
                return None
            return self.statuses[row_idx].status.environment.name

        elif layout is DashboardLayout.list:
            if row_idx < 0 or row_idx >= len(self._list_rows):
                return None
            return self._list_rows[row_idx][0]

        return None

    def get_selected_repo(self) -> str | None:
        """Return the repo name for the cursor cell, adapting to the active layout."""
        if self.cursor_coordinate is None:
            return None
        if not self.statuses:
            return None

        layout = self._active_layout()
        row_idx = self.cursor_coordinate.row
        col_idx = self.cursor_coordinate.column

        if layout is DashboardLayout.repos_as_rows:
            # rows = repos; col 0 = label col so any col maps to the same row's repo
            col_idx_env = col_idx - 1 if col_idx >= 1 else 0
            if col_idx_env >= len(self.statuses):
                return None
            repo_statuses = self.statuses[col_idx_env].repo_statuses
            if row_idx < 0 or row_idx >= len(repo_statuses):
                return None
            return repo_statuses[row_idx].worktree.repository.name

        elif layout is DashboardLayout.repos_as_columns:
            # cols 1..N = repos
            if col_idx < 1:
                return None
            repo_idx = col_idx - 1
            if repo_idx >= len(self._repo_keys):
                return None
            return self._repo_keys[repo_idx]

        elif layout is DashboardLayout.list:
            if row_idx < 0 or row_idx >= len(self._list_rows):
                return None
            return self._list_rows[row_idx][1]

        return None
