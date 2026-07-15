"""Quitting the dashboard cancels in-flight refresh workers without an error burst.

Issue #154 acceptance: quit cancels all refresh workers, exits without waiting on
in-flight git ops, and emits no worker-originated errors; `call_from_thread`
callbacks are no-ops once the app is tearing down.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from winter_cli.container import Container
from winter_cli.modules.tui import screens
from winter_cli.modules.tui.app import WinterDashboardApp
from winter_cli.modules.tui.screens.plugin_action_mixin import PluginActionMixin
from winter_cli.modules.tui.screens.workspace import WorkspaceScreen
from winter_cli.modules.workspace.workspace_snapshot_service import DashboardRefreshData


class _Host(PluginActionMixin):
    """Minimal PluginActionMixin host with a stand-in app for guard unit tests."""

    def __init__(self, app: object) -> None:
        self.app = app  # type: ignore[assignment]


def _app(*, is_running: bool) -> MagicMock:
    app = MagicMock()
    app.is_running = is_running
    return app


def test_call_from_thread_safe_noops_when_app_stopped() -> None:
    """After teardown (`is_running` False), the marshal is a silent no-op."""
    app = _app(is_running=False)
    host = _Host(app)

    result = host._call_from_thread_safe(MagicMock(name="callback"))

    assert result is None
    app.call_from_thread.assert_not_called()


def test_call_from_thread_safe_delegates_when_running() -> None:
    """While running (and no active worker), it marshals through call_from_thread."""
    app = _app(is_running=True)
    app.call_from_thread.return_value = "ok"
    host = _Host(app)
    cb = MagicMock(name="callback")

    result = host._call_from_thread_safe(cb, 1, x=2)

    assert result == "ok"
    app.call_from_thread.assert_called_once_with(cb, 1, x=2)


def test_call_from_thread_safe_noops_when_worker_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled worker does not touch the UI even while the app still runs."""
    app = _app(is_running=True)
    host = _Host(app)
    monkeypatch.setattr(
        screens.plugin_action_mixin, "get_current_worker", lambda: MagicMock(is_cancelled=True)
    )

    result = host._call_from_thread_safe(MagicMock(name="callback"))

    assert result is None
    app.call_from_thread.assert_not_called()


def test_call_from_thread_safe_swallows_teardown_race() -> None:
    """If the app stops between the check and the call, the RuntimeError is swallowed."""
    app = _app(is_running=True)
    app.call_from_thread.side_effect = RuntimeError("App is not running")
    host = _Host(app)

    result = host._call_from_thread_safe(MagicMock(name="callback"))

    assert result is None  # no exception escapes


@pytest.mark.asyncio
async def test_quit_cancels_in_flight_refresh_worker(container: Container) -> None:
    """A refresh worker mid-git is cancelled on quit, with no error logged as it unwinds."""
    log = container.error_log_svc()
    app = WinterDashboardApp(container)
    entered = threading.Event()
    release = threading.Event()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        screen = app.screen
        assert isinstance(screen, WorkspaceScreen)
        log.clear()

        def blocking_collect(*args, **kwargs):
            # Simulate an in-flight git probe: block until the test releases it.
            entered.set()
            release.wait(timeout=5)
            return DashboardRefreshData(overviews=[], standalone_statuses=[], main_statuses={})

        screen._snapshot_svc.collect_for_dashboard = blocking_collect  # type: ignore[assignment]

        screen.action_refresh()
        # Poll (yielding to the event loop, so the worker thread can be
        # dispatched) until the worker is genuinely blocked inside the probe.
        for _ in range(100):
            if entered.is_set():
                break
            await pilot.pause(0.05)
        assert entered.is_set(), "refresh worker never entered the blocking collect"
        workers = list(app.workers)

        await app.action_quit()
        # Let the blocked worker unwind now that quit has cancelled it.
        release.set()
        await pilot.pause(0.3)

    # The in-flight refresh worker was cancelled by quit.
    assert any(w.is_cancelled for w in workers), "expected the refresh worker to be cancelled"
    # The app is no longer running, and the worker's post-teardown callbacks
    # were no-ops — nothing was logged as it unwound.
    assert app.is_running is False
    assert log.entries() == []
