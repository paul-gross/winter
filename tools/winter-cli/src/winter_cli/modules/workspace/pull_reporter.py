from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from winter_cli.modules.workspace.models import SyncResult


class IPullReporter(Protocol):
    """Protocol for reporters that observe `ws pull` events as they happen."""

    def pull_started(self) -> None: ...
    def pull_completed(self, success: bool) -> None: ...
    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        ahead: int,
        behind: int,
    ) -> None: ...
    def env_skipped(self, env: str, reason: str) -> None: ...


class StreamPullReporter:
    """Renders pull events as human-readable text to stdout as work happens.

    Thread-safe: each event acquires a lock so individual lines stay atomic
    when WorkspaceSyncService runs git operations for multiple repos concurrently.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _echo(self, message: str, err: bool = False) -> None:
        with self._lock:
            self._click.echo(message, err=err)

    def pull_started(self) -> None:
        self._echo("→ pulling")

    def pull_completed(self, success: bool) -> None:
        if success:
            self._echo("\n✓ pull complete")
        else:
            self._echo("\n✗ pull had errors", err=True)

    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        ahead: int,
        behind: int,
    ) -> None:
        prefix = f"[{scope_label}/{repo_name}]"
        if result == SyncResult.diverged:
            self._echo(f"{prefix} diverged: +{ahead}/-{behind}", err=True)
        elif result == SyncResult.no_upstream:
            self._echo(f"{prefix} no upstream", err=True)
        elif result == SyncResult.merged:
            self._echo(f"{prefix} merged (merge commit created)")
        elif result == SyncResult.rebased:
            self._echo(f"{prefix} rebased (local commits replayed on upstream)")
        else:
            self._echo(f"{prefix} {result.value}")

    def env_skipped(self, env: str, reason: str) -> None:
        self._echo(f"[{env}] skipped: {reason}", err=True)


class JsonPullReporter:
    """Emits pull events as ndjson (one JSON object per line) to stdout.

    Thread-safe: each event is serialized and emitted under a lock so
    concurrent pulls don't produce interleaved JSON.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._click.echo(json.dumps(payload))

    def pull_started(self) -> None:
        self._emit({"type": "pull_started"})

    def pull_completed(self, success: bool) -> None:
        self._emit({"type": "pull_completed", "success": success})

    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        ahead: int,
        behind: int,
    ) -> None:
        self._emit(
            {
                "type": "repo_synced",
                "scope": scope_label,
                "repo": repo_name,
                "result": result.value,
                "ahead": ahead,
                "behind": behind,
            }
        )

    def env_skipped(self, env: str, reason: str) -> None:
        self._emit({"type": "env_skipped", "env": env, "reason": reason})


def _conforms_stream_pull_reporter(x: StreamPullReporter) -> IPullReporter:
    return x


def _conforms_json_pull_reporter(x: JsonPullReporter) -> IPullReporter:
    return x
