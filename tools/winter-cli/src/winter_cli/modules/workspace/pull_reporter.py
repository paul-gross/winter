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
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None: ...
    def env_skipped(self, env: str, reason: str) -> None: ...
    def source_checkout_warning(self, repo_name: str, warning: str) -> None: ...


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
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None:
        prefix = f"[{scope_label}/{repo_name}]"
        if result == SyncResult.diverged:
            self._echo(f"{prefix} diverged: +{ahead}/-{behind}", err=True)
        elif result == SyncResult.pin_error:
            msg = pin_ref if pin_ref else "pin operation failed"
            self._echo(f"{prefix} pin error: {msg}", err=True)
        elif result == SyncResult.no_upstream:
            # Benign for a feature worktree (excluded from the env's success
            # calc) → stdout; a real failure for a standalone → stderr.
            self._echo(f"{prefix} no upstream", err=scope_label == "standalone")
        elif result == SyncResult.up_to_date:
            self._echo(f"{prefix} up to date")
        elif result == SyncResult.merged:
            self._echo(f"{prefix} merged (+{commits})")
        elif result == SyncResult.rebased:
            self._echo(f"{prefix} rebased (+{commits})")
        elif result == SyncResult.held_pin:
            self._echo(f"{prefix} held @ {pin_ref}")
        elif result == SyncResult.re_pinned:
            self._echo(f"{prefix} re-pinned → {pin_ref}")
        elif result == SyncResult.pinned:
            self._echo(f"{prefix} pinned → {pin_ref}")
        elif result == SyncResult.already_pinned:
            self._echo(f"{prefix} already pinned @ {pin_ref}")
        elif result == SyncResult.refused:
            self._echo(f"{prefix} refused: {pin_ref}", err=True)
        else:
            self._echo(f"{prefix} fast-forwarded (+{commits})")

    def env_skipped(self, env: str, reason: str) -> None:
        self._echo(f"[{env}] skipped: {reason}", err=True)

    def source_checkout_warning(self, repo_name: str, warning: str) -> None:
        self._echo(f"[project/{repo_name}] warning: {warning}", err=True)


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
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None:
        self._emit(
            {
                "type": "repo_synced",
                "scope": scope_label,
                "repo": repo_name,
                "result": result.value,
                "commits": commits,
                "ahead": ahead,
                "behind": behind,
                "pin_ref": pin_ref,
            }
        )

    def env_skipped(self, env: str, reason: str) -> None:
        self._emit({"type": "env_skipped", "env": env, "reason": reason})

    def source_checkout_warning(self, repo_name: str, warning: str) -> None:
        self._emit({"type": "source_checkout_warning", "repo": repo_name, "warning": warning})


def _conforms_stream_pull_reporter(x: StreamPullReporter) -> IPullReporter:
    return x


def _conforms_json_pull_reporter(x: JsonPullReporter) -> IPullReporter:
    return x
