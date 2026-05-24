from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from winter_cli.modules.workspace.models import MergeResult


class IMergeReporter(Protocol):
    """Protocol for reporters that observe `ws merge` events as they happen.

    Shape mirrors `IPullReporter` with two differences: the start event
    carries the explicit `source_ref` (pull's source is always the tracked
    upstream), and the result enum is `MergeResult` instead of `SyncResult`
    (adds `skipped_missing_ref` for the merge-only case).
    """

    def merge_started(self, source_ref: str) -> None: ...
    def merge_completed(self, success: bool) -> None: ...
    def repo_merged(
        self,
        scope_label: str,
        repo_name: str,
        result: MergeResult,
        ahead: int,
        behind: int,
    ) -> None: ...


class StreamMergeReporter:
    """Renders merge events as human-readable text to stdout as work happens.

    Thread-safe: each event acquires a lock so individual lines stay atomic
    when the merge service runs git operations for multiple repos concurrently.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _echo(self, message: str, err: bool = False) -> None:
        with self._lock:
            self._click.echo(message, err=err)

    def merge_started(self, source_ref: str) -> None:
        self._echo(f"→ merging {source_ref}")

    def merge_completed(self, success: bool) -> None:
        if success:
            self._echo("\n✓ merge complete")
        else:
            self._echo("\n✗ merge had errors", err=True)

    def repo_merged(
        self,
        scope_label: str,
        repo_name: str,
        result: MergeResult,
        ahead: int,
        behind: int,
    ) -> None:
        prefix = f"[{scope_label}/{repo_name}]"
        if result == MergeResult.diverged:
            self._echo(f"{prefix} diverged: +{ahead}/-{behind}", err=True)
        elif result == MergeResult.skipped_missing_ref:
            self._echo(f"{prefix} skipped: source ref not found", err=True)
        elif result == MergeResult.merged:
            self._echo(f"{prefix} merged (merge commit created)")
        elif result == MergeResult.fast_forwarded:
            self._echo(f"{prefix} fast-forwarded")
        elif result == MergeResult.up_to_date:
            self._echo(f"{prefix} up-to-date")
        else:
            self._echo(f"{prefix} {result.value}")


class JsonMergeReporter:
    """Emits merge events as ndjson (one JSON object per line) to stdout.

    Thread-safe: each event is serialized and emitted under a lock so
    concurrent merges don't produce interleaved JSON.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._click.echo(json.dumps(payload))

    def merge_started(self, source_ref: str) -> None:
        self._emit({"type": "merge_started", "source_ref": source_ref})

    def merge_completed(self, success: bool) -> None:
        self._emit({"type": "merge_completed", "success": success})

    def repo_merged(
        self,
        scope_label: str,
        repo_name: str,
        result: MergeResult,
        ahead: int,
        behind: int,
    ) -> None:
        self._emit(
            {
                "type": "repo_merged",
                "scope": scope_label,
                "repo": repo_name,
                "result": result.value,
                "ahead": ahead,
                "behind": behind,
            }
        )


def _conforms_stream_merge_reporter(x: StreamMergeReporter) -> IMergeReporter:
    return x


def _conforms_json_merge_reporter(x: JsonMergeReporter) -> IMergeReporter:
    return x
