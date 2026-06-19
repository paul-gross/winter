"""Worktree detail screen opens focused on the repo the matrix cursor was on.

Issue/17 acceptance: pressing Enter on repo R in env E opens E's detail screen
with R's row selected and the per-repo info panel showing R. Also pins the
`get_selected_repo()` fix — it must resolve the repo from the *selected
column's* env, not always `statuses[0]`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings import KeybindingResolver
from winter_cli.modules.tui.screens.workspace.feature_worktrees import FeatureWorktreesGrid
from winter_cli.modules.tui.screens.worktree_detail.screen import WorktreeDetailScreen
from winter_cli.modules.workspace.models.domain_model import (
    FeatureEnvironment,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    Workspace,
)
from winter_cli.modules.workspace.models.service_model import (
    FeatureEnvironmentOverview,
    FeatureEnvironmentStatus,
    RepoStatus,
    WorktreeRepoStatus,
)

_WORKSPACE = Workspace(root_path=Path("/tmp/ws"), session_prefix="t", main_branch="main")


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


# --- get_selected_repo() -----------------------------------------------------


class _GridApp(App):
    def __init__(self, statuses: list[FeatureEnvironmentOverview]) -> None:
        super().__init__()
        self._statuses = statuses

    def compose(self) -> ComposeResult:
        yield FeatureWorktreesGrid(id="grid")

    def on_mount(self) -> None:
        self.query_one("#grid", FeatureWorktreesGrid).statuses = self._statuses


@pytest.mark.asyncio
async def test_get_selected_repo_resolves_from_selected_column_env():
    # Env "beta" is missing repo "c"; rows come from the first env ("alpha").
    statuses = [
        _overview("alpha", 1, ["a", "b", "c"]),
        _overview("beta", 2, ["a", "b"]),
    ]
    app = _GridApp(statuses)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # Beta column (col 2), repo row 1 -> beta's "b".
        grid.move_cursor(row=1, column=2)
        assert grid.get_selected_repo() == "b"

        # Beta column, row 2: beta has no repo there, so it resolves to None
        # rather than leaking alpha's "c" (the old statuses[0] bug).
        grid.move_cursor(row=2, column=2)
        assert grid.get_selected_repo() is None

        # Alpha column (col 1), row 2 -> alpha's "c".
        grid.move_cursor(row=2, column=1)
        assert grid.get_selected_repo() == "c"


@pytest.mark.asyncio
async def test_get_selected_repo_label_column_is_sensible_default():
    statuses = [_overview("alpha", 1, ["a", "b", "c"])]
    app = _GridApp(statuses)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)
        # Leftmost "Repositories" label column (col 0) must not crash.
        grid.move_cursor(row=1, column=0)
        assert grid.get_selected_repo() == "b"


# --- WorktreeDetailScreen focus seeding --------------------------------------


class _FakePluginRegistry:
    worktree_repo_decorators: tuple = ()
    environment_decorators: tuple = ()
    tui_actions: tuple = ()

    def __init__(self, detail_panels: tuple = ()) -> None:
        self.detail_panels = detail_panels

    def actions_for_scope(self, _scope):
        return []


class _CapturingPanel:
    """Records the DetailPanelContext it was rendered with."""

    name = "demo"
    title = "Demo"

    def __init__(self, captured: list) -> None:
        self._captured = captured

    def render(self, context) -> object:
        self._captured.append(context)
        return "[bold]panel body[/bold]"


class _FakeRepoFactory:
    def get_project_repos(self):
        return []


class _FakeWorkspaceRepo:
    def __init__(self, env: FeatureEnvironment) -> None:
        self._env = env

    def get_environment(self, _workspace, _name):
        return self._env


class _FakeEnvStatusSvc:
    def __init__(self, env: FeatureEnvironment, repo_statuses: list[WorktreeRepoStatus]) -> None:
        self._env = env
        self._repo_statuses = repo_statuses

    def get_environment_status(self, _env, _project_repos, _decorators):
        return FeatureEnvironmentStatus(environment=self._env, feature_branch="feature/alpha")

    def get_feature_environment_worktrees(self, _env, _project_repos):
        return FeatureEnvironmentWorktrees(
            environment=self._env,
            worktrees=[rs.worktree for rs in self._repo_statuses],
        )

    def get_worktree_repo_statuses(self, _env_worktrees, _decorators, on_repo_error=None):
        return self._repo_statuses


class _FakeRepoRepo:
    def get_worktree_status(self, wt: FeatureWorktree) -> RepoStatus:
        return RepoStatus(name=wt.repository.name, path=str(wt.path), main_branch="main")


class _DetailApp(App):
    def __init__(self, screen: WorktreeDetailScreen) -> None:
        super().__init__()
        self._detail_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._detail_screen)


def _make_detail_screen(focused_repo: str | None, detail_panels: tuple = ()) -> WorktreeDetailScreen:
    env = _env("alpha", 1)
    repo_statuses = [
        WorktreeRepoStatus(worktree=_worktree(env, rn), branch="alpha", ahead=0, behind=0, dirty_count=0)
        for rn in ("a", "b", "c")
    ]
    # The screen ctor types these seams as concrete services; the fakes
    # implement only the slice the refresh/detail path touches, so cast at
    # the construction edge (per testing.md's orchestration-edge guidance).
    return WorktreeDetailScreen(
        worktree_name="alpha",
        env_status_svc=cast(Any, _FakeEnvStatusSvc(env, repo_statuses)),
        workspace_repo=cast(Any, _FakeWorkspaceRepo(env)),
        repo_repo=cast(Any, _FakeRepoRepo()),
        repo_factory=cast(Any, _FakeRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _FakePluginRegistry(detail_panels)),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
        focused_repo=focused_repo,
    )


@pytest.mark.asyncio
async def test_detail_screen_opens_on_supplied_focused_repo():
    screen = _make_detail_screen(focused_repo="b")
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        assert screen._focused_repo == "b"
        table = screen.query_one("#detail-repos", DataTable)
        assert table.cursor_row == 1
        # Per-repo info panel loaded the focused repo, not the first.
        assert screen._repo_detail is not None
        assert screen._repo_detail.name == "b"


@pytest.mark.asyncio
async def test_detail_screen_falls_back_when_focused_repo_absent():
    screen = _make_detail_screen(focused_repo="does-not-exist")
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        assert screen._focused_repo == "a"
        table = screen.query_one("#detail-repos", DataTable)
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_detail_screen_defaults_to_first_repo_when_none_supplied():
    screen = _make_detail_screen(focused_repo=None)
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        assert screen._focused_repo == "a"
        table = screen.query_one("#detail-repos", DataTable)
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_detail_screen_renders_contributed_panel_with_worktree_context():
    # A contributed panel renders in the feature-env detail view too, and is
    # handed a worktree-bearing DetailPanelContext (the standalone view hands a
    # repo-bearing one — see test_detail_panels.py).
    from textual.widgets import Static

    from winter_cli.modules.tui.widgets.repo_detail_view import RepoDetailView

    captured: list = []
    screen = _make_detail_screen(focused_repo="b", detail_panels=(_CapturingPanel(captured),))
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        assert any(ctx.worktree is not None and ctx.worktree.repository.name == "b" for ctx in captured)
        assert all(ctx.repo is None for ctx in captured)
        view = screen.query_one("#detail-info", RepoDetailView)
        assert "panel body" in str(view.query_one("#detail-panel-0", Static).render())
