from __future__ import annotations

import json
import threading
from typing import Any, Protocol


class IInitReporter(Protocol):
    """Protocol for reporters that observe init reconcile events as they happen."""

    def target_started(self, target: str) -> None: ...
    def target_completed(self, target: str, success: bool) -> None: ...
    def repo_action(self, repo: str, location: str, action: str, detail: str = "") -> None: ...
    def repo_error(self, repo: str, error: str) -> None: ...
    def cmd_started(self, repo: str, command: str) -> None: ...
    def cmd_output_line(self, repo: str, line: str) -> None: ...
    def cmd_completed(self, repo: str, command: str, returncode: int) -> None: ...


class StreamReporter:
    """Default reporter that renders init events as human-readable text to stdout.

    Thread-safe: each event acquires a lock so individual lines stay atomic when
    InitService runs reconcile work for multiple repos concurrently.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _echo(self, message: str, err: bool = False) -> None:
        with self._lock:
            self._click.echo(message, err=err)

    def target_started(self, target: str) -> None:
        self._echo(f"→ {target}")

    def target_completed(self, target: str, success: bool) -> None:
        if success:
            self._echo(f"✓ {target} done")
        else:
            self._echo(f"✗ {target} failed", err=True)

    def repo_action(self, repo: str, location: str, action: str, detail: str = "") -> None:
        if action == "cloned":
            self._echo(f"[{repo}] cloned to {location}")
        elif action == "exists":
            self._echo(f"[{repo}] exists at {location}")
        elif action == "worktree_created":
            self._echo(f"[{repo}] worktree created at {location}")
        elif action == "symlinked":
            self._echo(f"[{repo}] symlinked {detail}")
        elif action == "excludes_updated":
            self._echo(f"[{repo}] excludes updated: {detail}")
        elif action == "pinned_tracking_set":
            self._echo(f"[{repo}] pinned tracking set: {detail}")
        elif action == "extension_installed":
            self._echo(f"[{repo}] extension installed: {detail}")
        elif action == "extension_warning":
            self._echo(f"[{repo}] extension warning: {detail}", err=True)
        elif action == "workspace_excludes_updated":
            self._echo(f"[{repo}] workspace excludes updated: {detail}")
        elif action == "claudemd_updated":
            self._echo(f"[{repo}] CLAUDE.md updated: {detail}")
        elif action == "worktree_removed":
            suffix = f" ({detail})" if detail else ""
            self._echo(f"[{repo}] worktree removed at {location}{suffix}")
        elif action == "env_removed":
            self._echo(f"[{repo}] env directory removed at {location}")
        elif action == "would_remove_worktree":
            self._echo(f"[{repo}] would remove worktree at {location}")
        elif action == "would_remove_env":
            self._echo(f"[{repo}] would remove env directory at {location}")
        elif action == "would_remove_workspace_exclude":
            self._echo(f"[{repo}] would strip workspace exclude block: {detail}")
        elif action == "hook_ran":
            self._echo(f"[{repo}] {detail} ran")
        else:
            self._echo(f"[{repo}] {action} {detail}".rstrip())

    def repo_error(self, repo: str, error: str) -> None:
        self._echo(f"[{repo}] ERROR: {error}", err=True)

    def cmd_started(self, repo: str, command: str) -> None:
        self._echo(f"[{repo}] $ {command}")

    def cmd_output_line(self, repo: str, line: str) -> None:
        self._echo(f"[{repo}] {line}")

    def cmd_completed(self, repo: str, command: str, returncode: int) -> None:
        if returncode == 0:
            return
        self._echo(f"[{repo}] ✗ {command} (exit {returncode})", err=True)


class JsonReporter:
    """Reporter that emits init events as one JSON object per line (ndjson) to stdout.

    Thread-safe: each event is serialized and emitted under a lock so concurrent
    reconcile work doesn't produce interleaved JSON.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._click.echo(json.dumps(payload))

    def target_started(self, target: str) -> None:
        self._emit({"type": "target_started", "target": target})

    def target_completed(self, target: str, success: bool) -> None:
        self._emit({"type": "target_completed", "target": target, "success": success})

    def repo_action(self, repo: str, location: str, action: str, detail: str = "") -> None:
        self._emit(
            {
                "type": "repo_action",
                "repo": repo,
                "location": location,
                "action": action,
                "detail": detail,
            }
        )

    def repo_error(self, repo: str, error: str) -> None:
        self._emit({"type": "repo_error", "repo": repo, "error": error})

    def cmd_started(self, repo: str, command: str) -> None:
        self._emit({"type": "cmd_started", "repo": repo, "command": command})

    def cmd_output_line(self, repo: str, line: str) -> None:
        self._emit({"type": "cmd_output_line", "repo": repo, "line": line})

    def cmd_completed(self, repo: str, command: str, returncode: int) -> None:
        self._emit(
            {
                "type": "cmd_completed",
                "repo": repo,
                "command": command,
                "returncode": returncode,
            }
        )


def _conforms_stream_reporter(x: StreamReporter) -> IInitReporter:
    return x


def _conforms_json_reporter(x: JsonReporter) -> IInitReporter:
    return x
