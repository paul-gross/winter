from __future__ import annotations

import contextlib
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.widgets import DataTable
from textual.widgets.data_table import ColumnKey

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

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._env_keys: list[str] = []
        self._repo_keys: list[str] = []
        self._pinned_by_name: dict[str, bool] = {}

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

    def _structure_matches(self) -> bool:
        env_keys = [o.status.environment.name for o in self.statuses]
        repo_keys = [rs.worktree.repository.name for rs in self.statuses[0].repo_statuses] if self.statuses else []
        return env_keys == self._env_keys and repo_keys == self._repo_keys

    @staticmethod
    def _column_header(overview: FeatureEnvironmentOverview) -> Text:
        badges = " ".join(v for v in overview.status.extensions.values() if v)
        branch = overview.status.feature_branch_label(disconnected="—")
        name = overview.status.environment.name.capitalize()
        title = f"{name} {badges}".rstrip()
        return Text(f"{title:<12}\n{branch}")

    def watch_statuses(self) -> None:
        if len(self.statuses) == 0:
            self.clear(columns=True)
            self._env_keys = []
            self._repo_keys = []
            return

        if self._structure_matches():
            self._update_in_place()
            return

        self._full_rebuild()

    def _full_rebuild(self) -> None:
        self.clear(columns=True)

        self.add_column(f"{'Repositories':<40}", key="repo")
        for overview in self.statuses:
            self.add_column(self._column_header(overview), key=overview.status.environment.name)

        self._env_keys = [o.status.environment.name for o in self.statuses]
        self._repo_keys = []

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

    def _build_repo_label(self, prefix: str, repo_name: str) -> Text:
        label = Text(f"{prefix}{repo_name}")
        ms = self.main_statuses.get(repo_name)
        if ms is not None:
            label.append(" ")
            label.append_text(render_repo_cell(ms, include_extensions=False))
        return label

    def watch_main_statuses(self) -> None:
        if not self._repo_keys:
            return
        self._update_in_place()

    def _build_repo_lookup(self) -> dict[str, dict[str, WorktreeRepoStatus]]:
        lookup: dict[str, dict[str, WorktreeRepoStatus]] = {}
        for overview in self.statuses:
            by_name: dict[str, WorktreeRepoStatus] = {}
            for repo_status in overview.repo_statuses:
                by_name[repo_status.worktree.repository.name] = repo_status
            lookup[overview.status.environment.name] = by_name
        return lookup

    def get_selected_worktree(self) -> str | None:
        if self.cursor_coordinate is None:
            return None
        col_idx = self.cursor_coordinate.column
        if col_idx < 1 or len(self.statuses) == 0:
            return None
        wt_idx = col_idx - 1
        if wt_idx >= len(self.statuses):
            return None
        return self.statuses[wt_idx].status.environment.name

    def get_selected_repo(self) -> str | None:
        if self.cursor_coordinate is None:
            return None
        if len(self.statuses) == 0:
            return None
        # Resolve the repo from the selected column's env. Col 0 is the
        # "Repositories" label column with no env of its own, so fall back
        # to the first env there — repo rows align across envs.
        col_idx = self.cursor_coordinate.column
        env_idx = col_idx - 1 if col_idx >= 1 else 0
        if env_idx >= len(self.statuses):
            return None
        repo_statuses = self.statuses[env_idx].repo_statuses
        row_idx = self.cursor_coordinate.row
        if row_idx < 0 or row_idx >= len(repo_statuses):
            return None
        return repo_statuses[row_idx].worktree.repository.name
