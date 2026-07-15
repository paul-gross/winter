"""Shared plugin-action plumbing for dashboard screens.

Every screen that surfaces plugin `TuiAction`s repeats the same two pieces:
resolve the dynamic `action_plugin_<name>` attribute the Textual binding dispatch
looks up, and log a `RepoError` to the session log + toast without crashing. This
mixin owns those two; the per-scope context resolution (which differs by screen —
a worktree screen has a focused repo, the workspace screen reads its grid) stays
on each screen as `_run_plugin_action`.

Binding plugin action *keys* is no longer done here: keys (built-in and plugin)
are resolved from config and installed by `KeybindingMixin._install_keybindings`,
with plugin `TuiAction`s adapted via `keybindings.actions.plugin_action_bindings`.

Host requirements (a Textual `Screen` subclass provides all of these):
  - `self._plugin_registry` — the `PluginRegistry`
  - `self._error_log` — the `ErrorLogService`
  - `self.app` — from Textual
  - `_run_plugin_action(self, action_name: str) -> None` — the screen's dispatcher
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.worker import NoActiveWorker, get_current_worker

from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.workspace.models import RepoError
from winter_cli.plugins.loader import PluginRegistry


class PluginActionMixin:
    """Mixin supplying the screen-agnostic half of plugin-action handling."""

    _plugin_registry: PluginRegistry
    _error_log: ErrorLogService
    app: App  # provided by the Textual `Screen` host (see module docstring)

    def _worker_cancelled(self) -> bool:
        """True if the calling refresh worker has been cancelled (e.g. by quit).

        Lets a `@work(thread=True)` worker bail before starting — or between —
        git operations so a quit that cancels workers is not held up by work
        the user no longer wants. Outside a worker there is nothing to cancel.
        """
        try:
            return get_current_worker().is_cancelled
        except NoActiveWorker:
            return False

    def _call_from_thread_safe(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Marshal a worker callback onto the UI thread; a no-op once tearing down.

        Refresh runs in `@work(thread=True)` workers whose git probes can still be
        mid-flight when the user quits. On quit the app cancels its workers and
        tears down its event loop; a `call_from_thread` that lands after that
        raises (the loop is gone) and — worse — blocks the worker on a UI
        round-trip that never completes, which is what makes quit slow and
        spews the trailing error burst. Skip the call when this worker has been
        cancelled or the app has stopped running, and swallow the teardown-race
        error if the app stops between the check and the call. A late callback
        thus becomes a silent no-op instead of an error.
        """
        try:
            if get_current_worker().is_cancelled:
                return None
        except NoActiveWorker:
            # Called outside a worker (e.g. direct UI-thread path) — no
            # cancellation to honor; fall through to the running-state guard.
            pass
        app = self.app
        if not app.is_running:
            return None
        try:
            return app.call_from_thread(callback, *args, **kwargs)
        except (RuntimeError, concurrent.futures.CancelledError):
            # App began tearing down between the is_running check and the call.
            return None

    def _capture_error(self, location: str, exc: RepoError, *, title: str = "git error") -> None:
        """Log a RepoError to the session log and toast (deduped) without crashing.

        Called from refresh/detail worker threads, so the toast is marshaled onto
        the UI thread via `_call_from_thread_safe` (a no-op once the app is tearing
        down, so a failure captured during quit never sprays an error). `title`
        defaults to "git error" since most captured RepoErrors originate from a git
        subcommand failure; callers wrapping a non-git failure (e.g. a config-parse
        error reused via RepoError to share this capture path) should pass a more
        accurate category.
        """
        entry, should_notify = self._error_log.record(location=location, exc=exc)
        if should_notify:
            self._call_from_thread_safe(
                self.app.notify,
                f"{entry.message}\nPress L for log",
                title=title,
                severity="error",
                timeout=6,
            )

    def __getattr__(self, name: str):
        # Textual resolves a binding named `plugin_<x>` to `action_plugin_<x>`;
        # synthesize that handler so screens declare actions without a method each.
        if name.startswith("action_plugin_"):
            action_name = name[len("action_plugin_") :]

            def handler() -> None:
                self._run_plugin_action(action_name)  # type: ignore[attr-defined]

            return handler
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
