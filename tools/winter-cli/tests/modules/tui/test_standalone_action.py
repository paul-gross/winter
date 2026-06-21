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
from winter_cli.modules.tui.screens.workspace.feature_worktrees import FeatureWorktreesGrid
from winter_cli.modules.tui.screens.workspace.screen import WorkspaceScreen
from winter_cli.modules.tui.screens.workspace.standalone_repos import StandaloneReposTable
from winter_cli.modules.workspace.models.domain_model import (
    FeatureEnvironment,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    StandaloneRepository,
    Workspace,
)
from winter_cli.modules.workspace.models.service_model import (
    FeatureEnvironmentOverview,
    FeatureEnvironmentStatus,
    StandaloneRepoStatus,
    WorktreeRepoStatus,
)
from winter_cli.plugins.types import (
    ActionInvocation,
    ActionScope,
    FeatureEnvironmentContext,
    FeatureWorktreeContext,
    StandaloneRepoContext,
    TuiAction,
)

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


class _FakeSnapshotSvc:
    """Minimal snapshot service fake — returns one singleton in standalone_statuses."""

    def collect_for_dashboard(self, **_kwargs: Any):
        from winter_cli.modules.workspace.workspace_snapshot_service import DashboardRefreshData

        return DashboardRefreshData(
            overviews=[],
            standalone_statuses=[StandaloneRepoStatus(repository=_REPO)],
            main_statuses={},
        )


class _FakePluginRegistry:
    worktree_repo_decorators: tuple = ()
    environment_decorators: tuple = ()

    def __init__(self, actions: list[TuiAction]) -> None:
        self.tui_actions = actions

    def actions_for_scope(self, scope: ActionScope) -> list[TuiAction]:
        return [a for a in self.tui_actions if scope in a.scopes]


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
        snapshot_svc=cast(Any, _FakeSnapshotSvc()),
        repo_factory=cast(Any, _FakeRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _FakePluginRegistry(actions)),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
    )


@pytest.mark.asyncio
async def test_standalone_action_fires_with_repo_context():
    captured: list[ActionInvocation] = []
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
    inv = captured[0]
    # Handlers now receive an ActionInvocation wrapping the context.
    assert isinstance(inv, ActionInvocation)
    assert inv.scope == ActionScope.standalone_repository
    # Attribute delegation preserves existing handler access patterns.
    assert isinstance(inv.context, StandaloneRepoContext)
    assert inv.repo.name == "winter-harness"


@pytest.mark.asyncio
async def test_standalone_action_is_noop_when_nothing_selected():
    captured: list[ActionInvocation] = []
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


# --- multi-scope dispatch (issue/58) -----------------------------------------


def _env(name: str, index: int) -> FeatureEnvironment:
    return FeatureEnvironment(workspace=_WORKSPACE, name=name, index=index, path=Path(f"/tmp/ws/{name}"))


def _worktree(env: FeatureEnvironment, repo_name: str) -> FeatureWorktree:
    repo = ProjectRepository(name=repo_name, main_path=Path(f"/tmp/ws/projects/{repo_name}"), main_branch="main")
    return FeatureWorktree(workspace=_WORKSPACE, environment=env, repository=repo)


def _overview(name: str, index: int, repo_names: list[str]) -> FeatureEnvironmentOverview:
    env = _env(name, index)
    repo_statuses = [
        WorktreeRepoStatus(worktree=_worktree(env, rn), branch=name, ahead=0, behind=0, dirty_count=0)
        for rn in repo_names
    ]
    status = FeatureEnvironmentStatus(environment=env, feature_branch=f"feature/{name}")
    return FeatureEnvironmentOverview(status=status, repo_statuses=repo_statuses)


class _FakeSnapshotSvcWithEnv:
    """Snapshot service that returns both a feature environment and a singleton."""

    def __init__(self, overview: FeatureEnvironmentOverview) -> None:
        self._overview = overview

    def collect_for_dashboard(self, **_kwargs: Any):
        from winter_cli.modules.workspace.workspace_snapshot_service import DashboardRefreshData

        return DashboardRefreshData(
            overviews=[self._overview],
            standalone_statuses=[StandaloneRepoStatus(repository=_REPO)],
            main_statuses={},
        )


class _FakeRepoFactoryWithWorktrees(_FakeRepoFactory):
    def __init__(self, env_worktrees: FeatureEnvironmentWorktrees) -> None:
        self._env_worktrees = env_worktrees

    def find_standalone(self, name):
        return next((r for r in [_REPO] if r.name == name), None)


def _make_screen_with_env(actions: list[TuiAction], overview: FeatureEnvironmentOverview) -> WorkspaceScreen:
    return WorkspaceScreen(
        snapshot_svc=cast(Any, _FakeSnapshotSvcWithEnv(overview)),
        repo_factory=cast(Any, _FakeRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _FakePluginRegistry(actions)),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
    )


@pytest.mark.asyncio
async def test_multiscope_action_fires_standalone_scope_when_singletons_focused():
    """A [feature_worktree, standalone_repository] action routes to standalone_repository
    when the singletons table holds focus, passing the correct ActionInvocation."""
    captured: list[ActionInvocation] = []
    action = TuiAction(
        name="probe",
        scope=[ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="P",
        description="probe",
        handler=captured.append,
    )
    overview = _overview("alpha", 1, ["winter-cli"])
    screen = _make_screen_with_env([action], overview)
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Focus the singletons table so the action routes to standalone_repository.
        table = screen.query_one("#singletons", StandaloneReposTable)
        table.focus()
        await pilot.pause()
        assert table.get_selected_repo() == "winter-harness"

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    inv = captured[0]
    assert isinstance(inv, ActionInvocation)
    assert inv.scope == ActionScope.standalone_repository
    assert isinstance(inv.context, StandaloneRepoContext)
    assert inv.repo.name == "winter-harness"


@pytest.mark.asyncio
async def test_multiscope_action_fires_worktree_scope_when_grid_focused():
    """A [feature_worktree, standalone_repository] action routes to feature_worktree
    when the grid table holds focus and a worktree+repo selection is resolvable."""
    captured: list[ActionInvocation] = []
    action = TuiAction(
        name="probe",
        scope=[ActionScope.feature_worktree, ActionScope.standalone_repository],
        key="P",
        description="probe",
        handler=captured.append,
    )
    overview = _overview("alpha", 1, ["winter-cli"])
    screen = _make_screen_with_env([action], overview)
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Focus the grid so the action routes to feature_worktree.
        # Move cursor to column 1 (first env, col 0 is the label column).
        grid = screen.query_one("#grid", FeatureWorktreesGrid)
        grid.move_cursor(row=0, column=1)
        grid.focus()
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "winter-cli"

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    inv = captured[0]
    assert isinstance(inv, ActionInvocation)
    assert inv.scope == ActionScope.feature_worktree
    assert isinstance(inv.context, FeatureWorktreeContext)
    assert inv.worktree.repository.name == "winter-cli"


# --- enriched action contexts (issue/82) -------------------------------------


@pytest.mark.asyncio
async def test_environment_action_context_carries_env_worktrees():
    """A feature_environment action receives the full per-env worktree set —
    each worktree's path, repository name, and branch (via environment.name) —
    populated from data already loaded at dispatch (no filesystem scan or config
    parsing). The workspace is reachable via `environment.workspace`."""
    captured: list[ActionInvocation] = []
    action = TuiAction(
        name="probe",
        scope=ActionScope.feature_environment,
        key="P",
        description="probe",
        handler=captured.append,
    )
    overview = _overview("alpha", 1, ["winter-cli", "winter-docs"])
    screen = _make_screen_with_env([action], overview)
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        grid = screen.query_one("#grid", FeatureWorktreesGrid)
        grid.move_cursor(row=0, column=1)
        grid.focus()
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    inv = captured[0]
    assert inv.scope == ActionScope.feature_environment
    assert isinstance(inv.context, FeatureEnvironmentContext)
    # Every project-repo worktree in the env is enumerable from the context.
    names = sorted(wt.repository.name for wt in inv.worktrees)
    assert names == ["winter-cli", "winter-docs"]
    cli_wt = next(wt for wt in inv.worktrees if wt.repository.name == "winter-cli")
    assert cli_wt.path == Path("/tmp/ws/alpha/winter-cli")
    assert cli_wt.environment.name == "alpha"
    # Workspace (root_path, main_branch) is reachable via the environment.
    assert inv.context.environment.workspace.root_path == Path("/tmp/ws")
    assert inv.context.environment.workspace.main_branch == "main"


@pytest.mark.asyncio
async def test_worktree_action_context_carries_siblings_and_workspace():
    """A feature_worktree action can reach the env's sibling worktrees (for
    env-wide actions triggered from a worktree cell) plus an explicit workspace
    handle, both populated from data already in hand at dispatch."""
    captured: list[ActionInvocation] = []
    action = TuiAction(
        name="probe",
        scope=ActionScope.feature_worktree,
        key="P",
        description="probe",
        handler=captured.append,
    )
    overview = _overview("alpha", 1, ["winter-cli", "winter-docs"])
    screen = _make_screen_with_env([action], overview)
    app = _ScreenApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        grid = screen.query_one("#grid", FeatureWorktreesGrid)
        grid.move_cursor(row=0, column=1)
        grid.focus()
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "winter-cli"

        screen._run_plugin_action("probe")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    inv = captured[0]
    assert isinstance(inv.context, FeatureWorktreeContext)
    assert inv.worktree.repository.name == "winter-cli"
    # Sibling worktrees are reachable for an env-wide action from a worktree cell.
    assert inv.context.environment_worktrees is not None
    sibling_names = sorted(wt.repository.name for wt in inv.context.environment_worktrees.worktrees)
    assert sibling_names == ["winter-cli", "winter-docs"]
    assert inv.context.environment_worktrees.environment.name == "alpha"
    # Explicit workspace handle (root_path, main_branch).
    workspace = inv.context.workspace
    assert workspace is not None
    assert workspace is _WORKSPACE
    assert workspace.main_branch == "main"
