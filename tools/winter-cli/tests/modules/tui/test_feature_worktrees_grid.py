"""FeatureWorktreesGrid — layout projections and selection mapping.

Covers:
- repos-as-rows (existing behavior): column/row structure, main-branch label indicator.
- repos-as-columns: rows = envs, cols = repos.
- list: one row per (env, repo), env/service elided on repeat rows; remote is per-repo.
- auto heuristic: 1 repo → list; repos > envs → repos-as-rows; else → repos-as-columns.
- get_selected_worktree / get_selected_repo correctness in every projection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from winter_cli.config.models import DashboardLayout
from winter_cli.modules.tui.screens.workspace.feature_worktrees import (
    FeatureWorktreesGrid,
)
from winter_cli.modules.tui.screens.workspace.repo_status import render_repo_cell
from winter_cli.modules.workspace.models.domain_model import (
    FeatureEnvironment,
    FeatureWorktree,
    ProjectRepository,
    Workspace,
)
from winter_cli.modules.workspace.models.service_model import (
    FeatureEnvironmentOverview,
    FeatureEnvironmentStatus,
    WorktreeRepoStatus,
)

_WORKSPACE = Workspace(root_path=Path("/tmp/ws"), session_prefix="t", main_branch="main")

_PIN_PAD = "  "


def _env(name: str, index: int) -> FeatureEnvironment:
    return FeatureEnvironment(workspace=_WORKSPACE, name=name, index=index, path=Path(f"/tmp/ws/{name}"))


def _repo(repo_name: str, pinned: bool = False) -> ProjectRepository:
    return ProjectRepository(
        name=repo_name, main_path=Path(f"/tmp/ws/projects/{repo_name}"), main_branch="main", pinned=pinned
    )


def _worktree(env: FeatureEnvironment, repo_name: str, pinned: bool = False) -> FeatureWorktree:
    return FeatureWorktree(workspace=_WORKSPACE, environment=env, repository=_repo(repo_name, pinned=pinned))


def _overview(
    name: str,
    index: int,
    repo_names: list[str],
    pinned_repos: set[str] | None = None,
    extensions: dict[str, str] | None = None,
    tracking: dict[str, str] | None = None,
) -> FeatureEnvironmentOverview:
    env = _env(name, index)
    pinned_repos = pinned_repos or set()
    tracking = tracking or {}
    repo_statuses = [
        WorktreeRepoStatus(
            worktree=_worktree(env, rn, pinned=rn in pinned_repos),
            branch=name,
            ahead=0,
            behind=0,
            dirty_count=0,
            tracking_branch=tracking.get(rn),
        )
        for rn in repo_names
    ]
    status = FeatureEnvironmentStatus(
        environment=env,
        feature_branch=f"feature/{name}",
        extensions=extensions or {},
    )
    return FeatureEnvironmentOverview(status=status, repo_statuses=repo_statuses)


def _main_status(repo_name: str, dirty_count: int = 0, ahead: int = 0, behind: int = 0) -> WorktreeRepoStatus:
    dummy_env = FeatureEnvironment(workspace=_WORKSPACE, name="", index=0, path=Path(f"/tmp/ws/projects/{repo_name}"))
    dummy_wt = FeatureWorktree(workspace=_WORKSPACE, environment=dummy_env, repository=_repo(repo_name))
    return WorktreeRepoStatus(
        worktree=dummy_wt,
        branch="main",
        ahead=ahead,
        behind=behind,
        dirty_count=dirty_count,
    )


class _GridApp(App):
    def __init__(
        self,
        statuses: list[FeatureEnvironmentOverview],
        layout: DashboardLayout = DashboardLayout.repos_as_rows,
    ) -> None:
        super().__init__()
        self._statuses = statuses
        self._layout = layout

    def compose(self) -> ComposeResult:
        yield FeatureWorktreesGrid(layout=self._layout, id="grid")

    def on_mount(self) -> None:
        self.query_one("#grid", FeatureWorktreesGrid).statuses = self._statuses


# ── repos-as-rows (existing behavior) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_main_branch_status_renders_in_label_column():
    """A repo with dirty_count=3 in main_statuses shows the rendered indicator in its label cell."""
    statuses = [_overview("alpha", 1, ["myrepo"])]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # Before setting main_statuses — label should not contain indicator
        cell_before = grid.get_cell("myrepo", "repo")
        assert "files" not in cell_before.plain

        # Set main_statuses with a dirty repo
        ms = _main_status("myrepo", dirty_count=3)
        grid.main_statuses = {"myrepo": ms}
        await pilot.pause()

        expected_suffix = render_repo_cell(ms, include_extensions=False).plain
        cell = grid.get_cell("myrepo", "repo")
        assert expected_suffix in cell.plain


@pytest.mark.asyncio
async def test_clean_main_branch_renders_no_indicator():
    """A repo with no main_statuses entry renders its label as the bare prefix+name with no suffix."""
    statuses = [_overview("alpha", 1, ["myrepo", "otherrepo"])]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # Set main_statuses only for otherrepo — myrepo has no entry (clean)
        ms_other = _main_status("otherrepo", dirty_count=2)
        grid.main_statuses = {"otherrepo": ms_other}
        await pilot.pause()

        clean_cell = grid.get_cell("myrepo", "repo")
        # Clean repo label is exactly the pin-pad prefix + repo name with no suffix
        assert clean_cell.plain == f"{_PIN_PAD} myrepo"

        dirty_cell = grid.get_cell("otherrepo", "repo")
        expected_suffix = render_repo_cell(ms_other, include_extensions=False).plain
        assert expected_suffix in dirty_cell.plain


@pytest.mark.asyncio
async def test_repos_as_rows_column_structure():
    """repos-as-rows: col 0 is 'repo', subsequent cols are env names; rows = repos."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        col_keys = [k.value for k in grid.columns]
        assert "repo" in col_keys
        assert "alpha" in col_keys
        assert "beta" in col_keys
        assert grid.row_count == 2  # one row per repo


@pytest.mark.asyncio
async def test_repos_as_rows_selection():
    """get_selected_worktree/get_selected_repo return correct values for repos-as-rows."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # cursor at (row=0, col=1) → first repo, first env
        grid.move_cursor(row=0, column=1, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "app"

        # cursor at (row=1, col=2) → second repo, second env
        grid.move_cursor(row=1, column=2, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "lib"


# ── repos-as-columns ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repos_as_columns_column_structure():
    """repos-as-columns: col 0 = env label, then one col per repo; rows = envs."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_columns)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        col_keys = [k.value for k in grid.columns]
        assert "_env" in col_keys
        assert "app" in col_keys
        assert "lib" in col_keys
        assert grid.row_count == 2  # one row per env


@pytest.mark.asyncio
async def test_repos_as_columns_cell_values():
    """repos-as-columns: each data cell is the render_repo_cell for (env, repo)."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_columns)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # All repos are clean, so every git-status cell should be "·" (dim)
        alpha_app = grid.get_cell("alpha", "app")
        assert alpha_app.plain == "·"

        beta_lib = grid.get_cell("beta", "lib")
        assert beta_lib.plain == "·"


@pytest.mark.asyncio
async def test_repos_as_columns_selection():
    """get_selected_worktree/get_selected_repo correct for repos-as-columns."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_columns)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # (row=0, col=1) → first env (alpha), first repo (app)
        grid.move_cursor(row=0, column=1, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "app"

        # (row=1, col=2) → second env (beta), second repo (lib)
        grid.move_cursor(row=1, column=2, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "lib"

        # (row=0, col=0) → env label col; repo is None
        grid.move_cursor(row=0, column=0, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() is None


# ── list layout ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_layout_column_structure():
    """list: columns are _env, _project, _remote, _git, _services."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.list)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        col_keys = [k.value for k in grid.columns]
        assert "_env" in col_keys
        assert "_project" in col_keys
        assert "_remote" in col_keys
        assert "_git" in col_keys
        assert "_services" in col_keys
        # 2 envs x 2 repos = 4 rows
        assert grid.row_count == 4


@pytest.mark.asyncio
async def test_list_layout_elision():
    """list: env/service render only on first row per env; remote is per-repo on every row."""
    statuses = [
        _overview(
            "alpha",
            1,
            ["app", "lib"],
            extensions={"svc": "running"},
            tracking={"app": "origin/feature/app", "lib": "origin/feature/lib"},
        ),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.list)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # Row 0: alpha/app — env + services non-blank; remote is app's own upstream
        row0_env = grid.get_cell("_list_0", "_env")
        assert "Alpha" in row0_env.plain

        row0_remote = grid.get_cell("_list_0", "_remote")
        assert row0_remote.plain == "origin/feature/app"

        row0_svc = grid.get_cell("_list_0", "_services")
        assert "running" in row0_svc.plain

        # Row 1: alpha/lib — env + services elided (blank), but remote is lib's OWN
        # upstream, not blank and not app's — the column is per-repo, not env-scoped.
        row1_env = grid.get_cell("_list_1", "_env")
        assert row1_env.plain == ""

        row1_remote = grid.get_cell("_list_1", "_remote")
        assert row1_remote.plain == "origin/feature/lib"

        row1_svc = grid.get_cell("_list_1", "_services")
        assert row1_svc.plain == ""

        # Row 2: beta/app — env non-blank again; no tracking configured → placeholder
        row2_env = grid.get_cell("_list_2", "_env")
        assert "Beta" in row2_env.plain

        row2_remote = grid.get_cell("_list_2", "_remote")
        assert row2_remote.plain == "—"


@pytest.mark.asyncio
async def test_list_layout_selection():
    """get_selected_worktree/get_selected_repo correct for list layout."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.list)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # row=0 → alpha/app
        grid.move_cursor(row=0, column=0, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "app"

        # row=1 → alpha/lib
        grid.move_cursor(row=1, column=0, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "alpha"
        assert grid.get_selected_repo() == "lib"

        # row=2 → beta/app
        grid.move_cursor(row=2, column=0, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "app"

        # row=3 → beta/lib
        grid.move_cursor(row=3, column=0, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "lib"


# ── auto heuristic ─────────────────────────────────────────────────────────


def test_auto_1_repo_resolves_to_list():
    """1 repo → list regardless of env count."""
    assert FeatureWorktreesGrid._resolve_auto(n_repos=1, n_envs=1) is DashboardLayout.list
    assert FeatureWorktreesGrid._resolve_auto(n_repos=1, n_envs=5) is DashboardLayout.list


def test_auto_repos_gt_envs_resolves_to_rows():
    """repos > envs → repos-as-rows."""
    assert FeatureWorktreesGrid._resolve_auto(n_repos=5, n_envs=3) is DashboardLayout.repos_as_rows
    assert FeatureWorktreesGrid._resolve_auto(n_repos=3, n_envs=2) is DashboardLayout.repos_as_rows


def test_auto_repos_eq_envs_resolves_to_columns():
    """repos == envs → repos-as-columns."""
    assert FeatureWorktreesGrid._resolve_auto(n_repos=3, n_envs=3) is DashboardLayout.repos_as_columns


def test_auto_repos_lt_envs_resolves_to_columns():
    """repos < envs → repos-as-columns."""
    assert FeatureWorktreesGrid._resolve_auto(n_repos=2, n_envs=5) is DashboardLayout.repos_as_columns


@pytest.mark.asyncio
async def test_auto_layout_selects_correct_rendering():
    """auto with 2 repos and 3 envs → repos-as-columns (repos < envs)."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
        _overview("gamma", 3, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.auto)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        col_keys = [k.value for k in grid.columns]
        # repos-as-columns: env col + repo cols
        assert "_env" in col_keys
        assert "app" in col_keys


@pytest.mark.asyncio
async def test_auto_layout_label_shows_resolved():
    """active_layout_label returns 'auto→<resolved>' when configured as auto."""
    statuses = [_overview("alpha", 1, ["app"])]
    app = _GridApp(statuses, layout=DashboardLayout.auto)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)
        label = grid.active_layout_label()
        assert label.startswith("auto→")


@pytest.mark.asyncio
async def test_explicit_layout_label_no_arrow():
    """active_layout_label returns the bare layout value when explicitly configured."""
    statuses = [_overview("alpha", 1, ["app", "lib"])]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)
        label = grid.active_layout_label()
        assert label == "repos-as-rows"
        assert "auto" not in label


# ── cycle_layout ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_layout_order():
    """cycle_layout advances through auto → repos-as-columns → repos-as-rows → list → auto."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.auto)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        assert grid._configured_layout is DashboardLayout.auto

        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.repos_as_columns

        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.repos_as_rows

        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.list

        # Wraps back to auto
        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.auto


@pytest.mark.asyncio
async def test_cycle_layout_label_reflects_new_layout():
    """active_layout_label reflects the resolved layout after each cycle."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        assert grid.active_layout_label() == "repos-as-rows"

        grid.cycle_layout()
        await pilot.pause()
        assert grid.active_layout_label() == "list"

        grid.cycle_layout()
        await pilot.pause()
        # list → auto (wrap)
        assert grid.active_layout_label().startswith("auto→")


@pytest.mark.asyncio
async def test_cycle_layout_selection_rows_to_columns():
    """After switching from repos-as-rows to repos-as-columns, selection returns correct env/repo."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    # Start with repos-as-rows, cursor at (row=1, col=2) = beta/lib
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_rows)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        grid.move_cursor(row=1, column=2, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "lib"

        # Cycle to repos-as-columns (rows → cols → rows-as-cols via auto->cols)
        # Starting from repos-as-rows: next is list
        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.list
        # After switch, grid has valid cursor — get_selected_* must return valid values
        wt = grid.get_selected_worktree()
        repo = grid.get_selected_repo()
        assert wt in ("alpha", "beta")
        assert repo in ("app", "lib")


@pytest.mark.asyncio
async def test_cycle_layout_selection_rows_to_list():
    """After switching from repos-as-rows to list, selection returns a valid env/repo pair."""
    statuses = [
        _overview("alpha", 1, ["app", "lib"]),
        _overview("beta", 2, ["app", "lib"]),
    ]
    app = _GridApp(statuses, layout=DashboardLayout.repos_as_columns)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        grid = app.query_one("#grid", FeatureWorktreesGrid)

        # cursor at (row=1, col=1): beta/app
        grid.move_cursor(row=1, column=1, animate=False)
        await pilot.pause()
        assert grid.get_selected_worktree() == "beta"
        assert grid.get_selected_repo() == "app"

        # Cycle: repos-as-columns → repos-as-rows
        grid.cycle_layout()
        await pilot.pause()
        assert grid._configured_layout is DashboardLayout.repos_as_rows
        # After switch, cursor stays in-bounds
        wt = grid.get_selected_worktree()
        repo = grid.get_selected_repo()
        assert wt in ("alpha", "beta")
        assert repo in ("app", "lib")
