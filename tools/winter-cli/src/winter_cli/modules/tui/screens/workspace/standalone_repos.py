from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.widgets import DataTable

from winter_cli.modules.workspace.models import StandaloneRepoStatus


class StandaloneReposTable(DataTable):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "cursor_left", "Left", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "cursor_right", "Right", show=False),
    ]

    statuses: reactive[list[StandaloneRepoStatus]] = reactive(list, always_update=True)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._repo_keys: list[str] = []

    def on_mount(self) -> None:
        self.cursor_type = "row"

    def action_cursor_down(self) -> None:
        row, col = self.cursor_coordinate
        if row >= self.row_count - 1:
            self.screen.query_one("#grid").focus()
            return
        self.move_cursor(row=row + 1, column=col, animate=False)

    def action_cursor_up(self) -> None:
        row, col = self.cursor_coordinate
        self.move_cursor(row=max(0, row - 1), column=col, animate=False)

    def get_selected_repo(self) -> str | None:
        """Return the selected standalone repo's name, or None if nothing is selected."""
        if self.cursor_coordinate is None:
            return None
        row_idx = self.cursor_coordinate.row
        if row_idx < 0 or row_idx >= len(self.statuses):
            return None
        return self.statuses[row_idx].name

    def watch_statuses(self) -> None:
        if len(self.statuses) == 0:
            self.clear(columns=True)
            self._repo_keys = []
            return

        repo_keys = [s.name for s in self.statuses]
        if repo_keys == self._repo_keys:
            self._update_in_place()
            return

        self._full_rebuild(repo_keys)

    def _full_rebuild(self, repo_keys: list[str]) -> None:
        self.clear(columns=True)
        self._repo_keys = repo_keys

        self.add_column(f"{'Repositories':<40}", key="repo")
        self.add_column(f"{'Branch':<20}", key="branch")
        self.add_column(f"{'Status':<20}", key="status")
        self.add_column("Latest Commit", key="commit")

        for s in self.statuses:
            commit = Text(s.latest_commit or "—", style="dim")
            self.add_row(
                s.name,
                s.branch or "—",
                self._render_status(s),
                commit,
                key=s.name,
            )

    def _update_in_place(self) -> None:
        for s in self.statuses:
            self.update_cell(s.name, "branch", s.branch or "—")
            self.update_cell(s.name, "status", self._render_status(s))
            self.update_cell(s.name, "commit", Text(s.latest_commit or "—", style="dim"))

    @staticmethod
    def _render_status(s: StandaloneRepoStatus) -> Text:
        parts: list[tuple[str, str]] = []

        if s.ahead > 0:
            parts.append((f"+{s.ahead}", "green"))
        if s.behind > 0:
            parts.append((f"-{s.behind}", "yellow"))
        if s.dirty_count == 1:
            parts.append(("1 file", "red"))
        elif s.dirty_count > 1:
            parts.append((f"{s.dirty_count} files", "red"))

        if len(parts) == 0 and s.tracking_ahead == 0:
            return Text("·", style="dim")

        text = Text()
        for i, (label, style) in enumerate(parts):
            if i > 0:
                text.append(" ")
            text.append(label, style=style)

        if s.tracking_ahead > 0:
            if len(parts) > 0:
                text.append(" ")
            text.append(f"[+{s.tracking_ahead}]", style="cyan")

        return text
