"""Dashboard captures RepoError without exiting, and the Log tab shows entries.

Issue/7 acceptance: "Dashboard captures RepoError as notifications and Log
entries without crashing" and "Log tab displays all required fields; entries
persist across screen switches."
"""
from __future__ import annotations

import pytest

from winter_cli.container import Container
from winter_cli.modules.tui.app import WinterDashboardApp
from winter_cli.modules.tui.screens.error_log import ErrorLogScreen
from winter_cli.modules.tui.screens.workspace import WorkspaceScreen
from winter_cli.modules.workspace.models import RepoError


def _err(message: str = "boom", **overrides) -> RepoError:
    defaults = dict(
        subcommand="fetch",
        args=("origin",),
        cwd="/tmp/r",
        exit_code=128,
        stderr="connection closed",
    )
    defaults.update(overrides)
    return RepoError(message, **defaults)


@pytest.mark.asyncio
async def test_log_screen_displays_injected_errors():
    container = Container()
    log = container.error_log_svc()
    app = WinterDashboardApp(container)
    async with app.run_test(size=(140, 40)) as pilot:
        # The initial refresh may surface real RepoErrors from whatever
        # workspace state the test runs against — clear after it settles so
        # the assertions below count only what we inject.
        await pilot.pause(0.5)
        log.clear()
        log.record(location="WorkspaceScreen.refresh", exc=_err("err A"))
        log.record(location="WorkspaceScreen.refresh", exc=_err("err A"))  # dup fingerprint
        log.record(location="WorkspaceScreen.sync(alpha)", exc=_err("err B", args=("upstream",)))
        await pilot.press("L")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ErrorLogScreen)
        # All three entries recorded (dedup is notifications-only).
        assert len(log.entries()) == 3
        # Pop back to workspace; entries survive navigation.
        await pilot.press("q")
        await pilot.pause(0.2)
        assert isinstance(app.screen, WorkspaceScreen)
        assert len(log.entries()) == 3
        # Re-open: still there.
        await pilot.press("L")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ErrorLogScreen)
        assert len(log.entries()) == 3


@pytest.mark.asyncio
async def test_log_clear_action_resets_log():
    container = Container()
    log = container.error_log_svc()
    log.clear()
    log.record(location="WorkspaceScreen.refresh", exc=_err())
    app = WinterDashboardApp(container)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("L")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ErrorLogScreen)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert log.entries() == []


@pytest.mark.asyncio
async def test_refresh_swallows_repo_error_into_log():
    """A RepoError raised inside the refresh worker is captured, not crashed.

    Substitutes a fake `_workspace_repo.get_environments` that raises, then
    drives a refresh. The dashboard stays alive and the log records the
    error with the screen-action location.
    """
    container = Container()
    log = container.error_log_svc()
    log.clear()
    app = WinterDashboardApp(container)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, WorkspaceScreen)

        # Inject a failure into the read path. The worker thread catches
        # RepoError via the wrapper added in issue/7.
        def boom(*args, **kwargs):
            raise _err("synthetic refresh failure", subcommand="status", args=())
        screen._workspace_repo.get_environments = boom  # type: ignore[assignment]

        screen.action_refresh()
        await pilot.pause(1.5)

        assert isinstance(app.screen, WorkspaceScreen)  # didn't crash
        entries = log.entries()
        assert any(e.location == "WorkspaceScreen.refresh" for e in entries)
        assert any(e.message == "synthetic refresh failure" for e in entries)
