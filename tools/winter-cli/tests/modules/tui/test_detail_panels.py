"""Plugin-contributed detail panels and the standalone detail drill-in (issue/19).

Covers the read-only detail-panel hook and the new standalone detail screen:

- `render_detail_panels` isolates a panel that raises and coerces non-renderables.
- `RepoDetailView` renders a bare info Static with zero panels (no tab bar) and a
  `TabbedContent` once a panel is contributed.
- `StandaloneDetailScreen` opens for a standalone repo, shows its `RepoStatus`,
  and renders a contributed panel with a `repo`-bearing `DetailPanelContext`.
- Enter on a standalone row drills into the standalone detail screen.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static, TabbedContent

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings import KeybindingResolver
from winter_cli.modules.tui.screens.standalone_detail import StandaloneDetailScreen
from winter_cli.modules.tui.screens.workspace.screen import WorkspaceScreen
from winter_cli.modules.tui.screens.workspace.standalone_repos import StandaloneReposTable
from winter_cli.modules.tui.widgets.repo_detail_view import (
    RepoDetailView,
    build_commit_graph,
    build_repo_info_markup,
    render_detail_panels,
)
from winter_cli.modules.workspace.models import RepoCommit, RepoStatus, StandaloneRepository, Workspace
from winter_cli.modules.workspace.models.service_model import StandaloneRepoStatus
from winter_cli.plugins.types import DetailPanelContext

_WORKSPACE = Workspace(root_path=Path("/tmp/ws"), session_prefix="t", main_branch="main")
_REPO = StandaloneRepository(name="winter-harness", path=Path("/tmp/ws/ai/harness"))


class _Panel:
    """Minimal IDetailPanel — records the context it was handed and returns markup."""

    def __init__(self, captured: list[DetailPanelContext], title: str = "Demo") -> None:
        self.name = "demo"
        self.title = title
        self._captured = captured

    def render(self, context: DetailPanelContext) -> object:
        self._captured.append(context)
        return "[bold]panel body[/bold]"


class _BoomPanel:
    name = "boom"
    title = "Boom"

    def render(self, context: DetailPanelContext) -> object:
        raise RuntimeError("kaboom")


# --- render_detail_panels ----------------------------------------------------


def test_render_detail_panels_passes_context_and_returns_content() -> None:
    captured: list[DetailPanelContext] = []
    ctx = DetailPanelContext(repo=_REPO)
    outcomes = render_detail_panels([_Panel(captured)], ctx)
    assert captured == [ctx]
    assert outcomes[0].error is None
    assert outcomes[0].content == "[bold]panel body[/bold]"


def test_render_detail_panels_isolates_a_raising_panel() -> None:
    captured: list[DetailPanelContext] = []
    outcomes = render_detail_panels([_BoomPanel(), _Panel(captured)], DetailPanelContext(repo=_REPO))
    # The exploding panel becomes an error outcome; the sibling still renders.
    assert outcomes[0].error == "kaboom"
    assert "Panel error" in str(outcomes[0].content)
    assert outcomes[1].error is None


def test_render_detail_panels_coerces_non_renderable_to_str() -> None:
    panel = SimpleNamespace(name="n", title="N", render=lambda _ctx: 1234)
    outcomes = render_detail_panels([cast(Any, panel)], DetailPanelContext(repo=_REPO))
    assert outcomes[0].content == "1234"


# --- build_repo_info_markup upstream wording ---------------------------------


def test_info_markup_no_upstream() -> None:
    detail = RepoStatus(name="r", path="/p", main_branch="main", tracking_branch=None)
    assert "Upstream: [dim]none[/dim]" in build_repo_info_markup(detail)


def test_info_markup_unborn_upstream() -> None:
    # tracking_ref_present == False — configured but never pushed/fetched.
    detail = RepoStatus(
        name="r",
        path="/p",
        main_branch="main",
        tracking_branch="origin/feature/x",
        tracking_ref_present=False,
    )
    markup = build_repo_info_markup(detail)
    assert "origin/feature/x configured, not yet pushed/fetched" in markup


def test_info_markup_tracked_and_present() -> None:
    detail = RepoStatus(
        name="r",
        path="/p",
        main_branch="main",
        tracking_branch="origin/feature/x",
        tracking_ref_present=True,
        tracking_ahead=2,
        tracking_behind=0,
    )
    markup = build_repo_info_markup(detail)
    assert "tracking origin/feature/x — ahead 2, behind 0" in markup


# --- build_commit_graph -------------------------------------------------------


def test_commit_graph_empty_state_names_main() -> None:
    detail = RepoStatus(name="r", path="/p", main_branch="main")
    assert "No commits beyond origin/main" in build_commit_graph(detail).plain


def test_commit_graph_renders_topology_lines() -> None:
    detail = RepoStatus(
        name="r",
        path="/p",
        main_branch="main",
        commit_graph=["* abc1234 work", "o def5678 base"],
    )
    plain = build_commit_graph(detail).plain
    assert "* abc1234 work" in plain
    assert "o def5678 base" in plain


# --- RepoDetailView -----------------------------------------------------------


class _ViewApp(App):
    def __init__(self, panels: list[Any]) -> None:
        super().__init__()
        self._panels = panels

    def compose(self) -> ComposeResult:
        yield RepoDetailView(self._panels, id="detail-info")


def _repo_status() -> RepoStatus:
    return RepoStatus(
        name="winter-harness",
        path="/tmp/ws/ai/harness",
        main_branch=None,
        branch="main",
        tracking_branch="origin/main",
        tracking_ref_present=True,
        recent_commits=[RepoCommit(short_hash="abc1234", message="recent work")],
        commit_graph=["* abc1234 recent work"],
    )


@pytest.mark.asyncio
async def test_view_with_zero_panels_has_no_tab_bar() -> None:
    app = _ViewApp([])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.query_one("#detail-info", RepoDetailView)
        assert len(view.query(TabbedContent)) == 0
        assert view.query_one("#repo-info", Static) is not None
        # The commit graph lives in a scrollable container so tall histories scroll.
        assert isinstance(view.query_one("#repo-graph-scroll"), VerticalScroll)
        assert view.query_one("#repo-graph", Static) is not None


@pytest.mark.asyncio
async def test_view_with_panels_renders_tabs_and_updates_content() -> None:
    captured: list[DetailPanelContext] = []
    app = _ViewApp([_Panel(captured)])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.query_one("#detail-info", RepoDetailView)
        assert len(view.query(TabbedContent)) == 1

        outcomes = render_detail_panels([_Panel(captured)], DetailPanelContext(repo=_REPO))
        view.show_repo(_repo_status(), outcomes)
        await pilot.pause()

        assert "recent work" in str(view.query_one("#repo-graph", Static).render())
        assert "panel body" in str(view.query_one("#detail-panel-0", Static).render())


@pytest.mark.asyncio
async def test_view_renders_panel_error_state() -> None:
    app = _ViewApp([_BoomPanel()])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.query_one("#detail-info", RepoDetailView)
        outcomes = render_detail_panels([_BoomPanel()], DetailPanelContext(repo=_REPO))
        view.show_repo(_repo_status(), outcomes)
        await pilot.pause()
        assert "Panel error" in str(view.query_one("#detail-panel-0", Static).render())


# --- StandaloneDetailScreen ---------------------------------------------------


class _FakeRepoFactory:
    def get_singleton_repos(self) -> list[StandaloneRepository]:
        return [_REPO]

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return []

    def find_standalone(self, name: str) -> StandaloneRepository | None:
        return next((r for r in [*self.get_singleton_repos(), *self.get_standalone_repos()] if r.name == name), None)


class _FakeRepoRepo:
    def __init__(self, detail: RepoStatus) -> None:
        self._detail = detail

    def get_standalone_detail(self, repo: StandaloneRepository) -> RepoStatus:
        return self._detail

    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        return StandaloneRepoStatus(repository=repo)


class _FakePluginRegistry:
    def __init__(self, panels: list[Any]) -> None:
        self.detail_panels = panels
        self.tui_actions: list[Any] = []

    def actions_for_scope(self, _scope) -> list[Any]:
        return []


class _DetailApp(App):
    def __init__(self, screen: StandaloneDetailScreen) -> None:
        super().__init__()
        self._detail_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._detail_screen)


def _make_standalone_screen(panels: list[Any]) -> StandaloneDetailScreen:
    return StandaloneDetailScreen(
        repo_name="winter-harness",
        repo_repo=cast(Any, _FakeRepoRepo(_repo_status())),
        repo_factory=cast(Any, _FakeRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _FakePluginRegistry(panels)),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
    )


@pytest.mark.asyncio
async def test_standalone_detail_shows_repo_and_panel() -> None:
    captured: list[DetailPanelContext] = []
    screen = _make_standalone_screen([_Panel(captured)])
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert screen._repo_detail is not None
        assert screen._repo_detail.name == "winter-harness"
        # The panel got a standalone (repo-bearing) context, not a worktree one.
        assert len(captured) == 1
        assert captured[0].repo is _REPO
        assert captured[0].worktree is None

        view = screen.query_one("#detail-info", RepoDetailView)
        assert "recent work" in str(view.query_one("#repo-graph", Static).render())
        assert "panel body" in str(view.query_one("#detail-panel-0", Static).render())


@pytest.mark.asyncio
async def test_standalone_detail_with_no_panels_renders_plain_info() -> None:
    screen = _make_standalone_screen([])
    app = _DetailApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = screen.query_one("#detail-info", RepoDetailView)
        assert len(view.query(TabbedContent)) == 0
        assert "recent work" in str(view.query_one("#repo-graph", Static).render())


# --- Enter on a standalone row drills in -------------------------------------


class _FakeScreenFactory:
    def __init__(self) -> None:
        self.standalone_calls: list[str] = []

    def standalone_detail_screen(self, repo_name: str) -> Screen:
        self.standalone_calls.append(repo_name)
        return Screen()


class _WsRepoFactory:
    def get_project_repos(self) -> list[Any]:
        return []

    def get_singleton_repos(self) -> list[StandaloneRepository]:
        return [_REPO]

    def get_standalone_repos(self) -> list[StandaloneRepository]:
        return []


class _WsWorkspaceRepo:
    def get_environments(self, _workspace, _project_repos) -> list[Any]:
        return []


class _WsRepoRepo:
    def get_standalone_status(self, repo: StandaloneRepository) -> StandaloneRepoStatus:
        return StandaloneRepoStatus(repository=repo)


class _WsPluginRegistry:
    worktree_repo_decorators: tuple = ()
    environment_decorators: tuple = ()
    tui_actions: tuple = ()

    def actions_for_scope(self, _scope) -> list[Any]:
        return []


class _WsApp(App):
    def __init__(self, screen: WorkspaceScreen) -> None:
        super().__init__()
        self._ws_screen = screen
        self.screen_factory = _FakeScreenFactory()

    def on_mount(self) -> None:
        self.push_screen(self._ws_screen)


def _make_workspace_screen() -> WorkspaceScreen:
    return WorkspaceScreen(
        env_status_svc=cast(Any, None),
        workspace_repo=cast(Any, _WsWorkspaceRepo()),
        repo_repo=cast(Any, _WsRepoRepo()),
        repo_factory=cast(Any, _WsRepoFactory()),
        workspace=_WORKSPACE,
        plugin_registry=cast(Any, _WsPluginRegistry()),
        error_log=cast(Any, None),
        keybinding_resolver=KeybindingResolver(KeybindingsConfig()),
    )


@pytest.mark.asyncio
async def test_enter_on_standalone_row_opens_detail_screen() -> None:
    screen = _make_workspace_screen()
    app = _WsApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = screen.query_one("#singletons", StandaloneReposTable)
        assert table.get_selected_repo() == "winter-harness"

        event = SimpleNamespace(data_table=SimpleNamespace(id="singletons"))
        screen.on_data_table_row_selected(cast(Any, event))
        await pilot.pause()

    assert app.screen_factory.standalone_calls == ["winter-harness"]


@pytest.mark.asyncio
async def test_row_selected_from_other_table_is_ignored() -> None:
    screen = _make_workspace_screen()
    app = _WsApp(screen)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        event = SimpleNamespace(data_table=SimpleNamespace(id="something-else"))
        screen.on_data_table_row_selected(cast(Any, event))
        await pilot.pause()

    assert app.screen_factory.standalone_calls == []
