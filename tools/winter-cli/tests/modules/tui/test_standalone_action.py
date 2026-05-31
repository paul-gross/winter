"""Standalone-repository action scope dispatch (issue/16).

Covers the new fourth `ActionScope`: a plugin action scoped to a standalone
repo fires with a `StandaloneRepoContext` for the selected repo, and is a no-op
when no standalone repo is selected (rather than crashing or mis-targeting).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings import KeybindingResolver
from winter_cli.modules.tui.screens.workspace.screen import WorkspaceScreen
from winter_cli.modules.tui.screens.workspace.standalone_repos import StandaloneReposTable
from winter_cli.modules.workspace.models.domain_model import StandaloneRepository, Workspace
from winter_cli.modules.workspace.models.service_model import StandaloneRepoStatus
from winter_cli.plugins.types import ActionContext, ActionScope, StandaloneRepoContext, TuiAction

_WORKSPACE = Workspace(root_path=Path("/tmp/ws"), session_prefix="t", main_branch="main")
_REPO = StandaloneRepository(name="winter-harness", path=Path("/tmp/ws/ai/harness"))


# --- get_selected_repo() -----------------------------------------------------


class _TableApp(App):
    def __init__(self, statuses: list[StandaloneRepoStatus]) -> None:
        super().__init__()
        self._statuses = statuses

    def compose(self) -> ComposeResult:
        yield StandaloneReposTable(id="singletons")

    def on_mount(self) -> None:
        self.query_one("#singletons", StandaloneReposTable).statuses = self._statuses


@pytest.mark.asyncio
async def test_get_selected_repo_returns_selected_row_name():
    statuses = [
        StandaloneRepoStatus(repository=StandaloneRepository(name="a", path=Path("/tmp/a"))),
        StandaloneRepoStatus(repository=StandaloneRepository(name="b", path=Path("/tmp/b"))),
    ]
    app = _TableApp(statuses)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#singletons", StandaloneReposTable)
        table.move_cursor(row=1, column=0)
        assert table.get_selected_repo() == "b"


@pytest.mark.asyncio
async def test_get_selected_repo_is_none_when_empty():
    app = _TableApp([])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#singletons", StandaloneReposTable)
        assert table.get_selected_repo() is None


# --- dispatch ----------------------------------------------------------------


class _FakeRepoFactory:
    def get_project_repos(self):
        return []

    def get_singleton_repos(self):
        return [_REPO]

    def get_standalone_repos(self):
        return []

    def find_standalone(self, name):
        return next((r for r in [*self.get_singleton_repos(), *self.get_standalone_repos()] if r.name == name), None)


class _FakeWorkspaceRepo:
    def get_environments(self, _workspace, _project_repos):
        return []


class _FakeRepoRepo:
    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        return StandaloneRepoStatus(repository=repo)


class _FakePluginRegistry:
    worktree_repo_decorators: tuple = ()
    environment_decorators: tuple = ()

    def __init__(self, actions: list[TuiAction]) -> None:
        self.tui_actions = actions

    def actions_for_scope(self, scope: ActionScope) -> list[TuiAction]:
        return [a for a in self.tui_actions if a.scope == scope]


class _ScreenApp(App):
    def __init__(self, screen: WorkspaceScreen) -> None:
        super().__init__()
        self._screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._screen)


def _make_screen(actions: list[TuiAction]) -> WorkspaceScreen:
    # The ctor types these seams as concrete services; the fakes implement only
    # the slice the refresh/dispatch path touches, so cast at the construction
    # edge (per testing.md's orchestration-edge guidance).
    return WorkspaceScreen(
        env_status_svc=cast(Any, None),
        workspace_repo=cast(Any, _FakeWorkspaceRepo()),
        repo_repo=cast(Any, _FakeRepoRepo()),
        repo_factory=cast(Any, _FakeRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _FakePluginRegistry(actions)),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
    )


@pytest.mark.asyncio
async def test_standalone_action_fires_with_repo_context():
    captured: list[ActionContext] = []
    action = TuiAction(
        name="probe",
        scope=ActionScope.standalone_repository,
        key="P",
        description="probe",
        handler=captured.append,
    )
    screen = _make_screen([action])
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        # Let the on-mount refresh worker populate the standalone table.
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = screen.query_one("#singletons", StandaloneReposTable)
        assert table.get_selected_repo() == "winter-harness"

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    ctx = captured[0]
    assert isinstance(ctx, StandaloneRepoContext)
    assert ctx.repo.name == "winter-harness"


@pytest.mark.asyncio
async def test_standalone_action_is_noop_when_nothing_selected():
    captured: list[ActionContext] = []
    action = TuiAction(
        name="probe",
        scope=ActionScope.standalone_repository,
        key="P",
        description="probe",
        handler=captured.append,
    )
    screen = _make_screen([action])
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Empty the standalone table so no repo is selected (e.g. matrix focused).
        table = screen.query_one("#singletons", StandaloneReposTable)
        table.statuses = []
        await pilot.pause()
        assert table.get_selected_repo() is None

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert captured == []
