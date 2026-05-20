from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timedelta

from winter_cli.modules.workspace.models import RepoError


@dataclasses.dataclass(frozen=True)
class ErrorLogEntry:
    """One captured RepoError, ready for the dashboard Log tab.

    Carries everything the issue requires the tab to show: timestamp, the
    screen/action that triggered the call, plus the structured fields from
    the underlying git failure (subcommand, args, cwd, exit code, stderr).
    """
    timestamp: datetime
    location: str
    message: str
    subcommand: str | None
    args: tuple[str, ...]
    cwd: str | None
    exit_code: int | None
    stderr: str

    def header(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S")
        return f"{ts}  {self.location}  —  {self.message}"

    def command_line(self) -> str:
        if not self.subcommand:
            return ""
        return " ".join(("$ git", self.subcommand, *self.args))


class ErrorLogService:
    """Session-scoped log of RepoErrors captured by the TUI.

    Persists across screen navigation within a single dashboard session
    (not across process restarts — that's out of scope per the issue).
    Maintains per-fingerprint notification timestamps so background polling
    that keeps failing the same way only toasts once every `notify_ttl`
    seconds, while every individual failure still lands in the log.
    """

    def __init__(self, *, notify_ttl_seconds: int = 30) -> None:
        self._entries: list[ErrorLogEntry] = []
        self._last_notify: dict[tuple[str | None, tuple[str, ...], str | None], datetime] = {}
        self._notify_ttl = timedelta(seconds=notify_ttl_seconds)
        self._lock = threading.Lock()

    def record(self, *, location: str, exc: RepoError) -> tuple[ErrorLogEntry, bool]:
        """Append a log entry; return (entry, should_notify).

        The caller is expected to read `should_notify` and only post a
        Textual toast when it's True — background pollers that hammer the
        same fingerprint stay quiet on subsequent failures, but the entry
        still lands in the log.
        """
        now = datetime.now()
        entry = ErrorLogEntry(
            timestamp=now,
            location=location,
            message=exc.message,
            subcommand=exc.subcommand,
            args=exc.args,
            cwd=exc.cwd,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
        )
        fingerprint = (exc.subcommand, exc.args, exc.cwd)
        with self._lock:
            self._entries.append(entry)
            last = self._last_notify.get(fingerprint)
            should_notify = last is None or (now - last) > self._notify_ttl
            if should_notify:
                self._last_notify[fingerprint] = now
        return entry, should_notify

    def entries(self) -> list[ErrorLogEntry]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._last_notify.clear()
