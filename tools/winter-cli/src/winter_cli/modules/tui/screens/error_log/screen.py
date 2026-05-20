from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from winter_cli.modules.tui.error_log import ErrorLogEntry, ErrorLogService


class ErrorLogScreen(Screen):
    """Session-scoped log of captured RepoErrors.

    Pushed by `L` from any dashboard screen and popped by `q`. Reads from
    the same `ErrorLogService` instance the polling/action workers write
    to, so navigating away and back preserves every entry recorded during
    the session.
    """

    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("c", "clear", "Clear"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, error_log: ErrorLogService, **kwargs) -> None:
        super().__init__(**kwargs)
        self._error_log = error_log
        self._selected_index: int | None = None

    def compose(self):
        yield Header()
        yield DataTable(id="error-log-table")
        with Vertical(id="error-log-detail"):
            yield Static(id="error-log-detail-text")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#error-log-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Time", key="ts")
        table.add_column("Where", key="loc")
        table.add_column("git", key="cmd")
        table.add_column("Exit", key="exit")
        table.add_column("Message", key="message")
        self._populate()

    def action_refresh(self) -> None:
        self._populate()

    def action_clear(self) -> None:
        self._error_log.clear()
        self._populate()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _populate(self) -> None:
        table = self.query_one("#error-log-table", DataTable)
        entries = self._error_log.entries()
        # Newest first so freshly-captured errors are visible on push.
        entries = list(reversed(entries))
        table.clear()
        if not entries:
            self._update_detail(None)
            return
        for i, entry in enumerate(entries):
            cmd_cell = entry.command_line().removeprefix("$ git ").strip() or "—"
            exit_cell = str(entry.exit_code) if entry.exit_code is not None else "—"
            table.add_row(
                entry.timestamp.strftime("%H:%M:%S"),
                entry.location,
                cmd_cell,
                exit_cell,
                entry.message,
                key=str(i),
            )
        self._entries_ordered = entries
        self._update_detail(entries[0])

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        idx = int(event.row_key.value)
        entries = getattr(self, "_entries_ordered", [])
        if 0 <= idx < len(entries):
            self._update_detail(entries[idx])

    def _update_detail(self, entry: ErrorLogEntry | None) -> None:
        detail = self.query_one("#error-log-detail-text", Static)
        if entry is None:
            detail.update("[dim]No errors captured in this session.[/dim]")
            return
        text = Text()
        text.append(entry.message + "\n", style="bold")
        text.append(f"when:   {entry.timestamp.isoformat(timespec='seconds')}\n")
        text.append(f"where:  {entry.location}\n")
        cmd = entry.command_line()
        if cmd:
            text.append(f"cmd:    {cmd}\n")
        if entry.cwd:
            text.append(f"cwd:    {entry.cwd}\n")
        if entry.exit_code is not None:
            text.append(f"exit:   {entry.exit_code}\n")
        if entry.stderr:
            text.append("stderr:\n", style="bold")
            text.append(entry.stderr.strip() + "\n")
        detail.update(text)
