from __future__ import annotations

import json
import threading
from typing import Any, Protocol


class IFetchReporter(Protocol):
    """Protocol for reporters that observe `ws fetch` events as they happen."""

    def fetch_started(self) -> None: ...
    def fetch_completed(self, success: bool) -> None: ...
    def repo_fetched(
        self,
        scope_label: str,
        repo_name: str,
        success: bool,
        error: str | None,
    ) -> None: ...


class StreamFetchReporter:
    """Renders fetch events as human-readable text to stdout as work happens.

    Thread-safe: each event acquires a lock so individual lines stay atomic
    when WorkspaceSyncService runs git operations for multiple repos concurrently.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _echo(self, message: str, err: bool = False) -> None:
        with self._lock:
            self._click.echo(message, err=err)

    def fetch_started(self) -> None:
        self._echo("→ fetching")

    def fetch_completed(self, success: bool) -> None:
        if success:
            self._echo("\n✓ fetch complete")
        else:
            self._echo("\n✗ fetch had errors", err=True)

    def repo_fetched(
        self,
        scope_label: str,
        repo_name: str,
        success: bool,
        error: str | None,
    ) -> None:
        prefix = f"[{scope_label}/{repo_name}]"
        if success:
            self._echo(f"{prefix} ok")
        else:
            self._echo(f"{prefix} error: {error or 'unknown error'}", err=True)


class JsonFetchReporter:
    """Emits fetch events as ndjson (one JSON object per line) to stdout.

    Thread-safe: each event is serialized and emitted under a lock so
    concurrent fetches don't produce interleaved JSON.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._click.echo(json.dumps(payload))

    def fetch_started(self) -> None:
        self._emit({"type": "fetch_started"})

    def fetch_completed(self, success: bool) -> None:
        self._emit({"type": "fetch_completed", "success": success})

    def repo_fetched(
        self,
        scope_label: str,
        repo_name: str,
        success: bool,
        error: str | None,
    ) -> None:
        self._emit(
            {
                "type": "repo_fetched",
                "scope": scope_label,
                "repo": repo_name,
                "success": success,
                "error": error,
            }
        )


def _conforms_stream_fetch_reporter(x: StreamFetchReporter) -> IFetchReporter:
    return x


def _conforms_json_fetch_reporter(x: JsonFetchReporter) -> IFetchReporter:
    return x
