from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, PropertyMock, patch

import click
import pytest

from winter_cli.modules.workspace.handlers.workspace_handler import (
    EnvFetchParams,
    EnvStatusParams,
    EnvWorktreesParams,
    WorkspaceHandler,
    compute_status_exit_code,
)
from winter_cli.modules.workspace.models import (
    DashboardSnapshot,
    EnvSnapshot,
    FetchReport,
    OrphanSnapshot,
    RepoFetchOutcome,
    RepoScope,
    SourceCheckoutSnapshot,
    StandaloneRepository,
    StandaloneRepoStatus,
    WorkspaceLevelSnapshot,
    WorkspaceSnapshot,
    WorktreeRepoStatus,
    WorktreeSnapshot,
)


def _make_handler(fetch_report: FetchReport) -> WorkspaceHandler:
    """Build a WorkspaceHandler with the minimum stubs fetch() touches."""
    workspace_sync_svc = MagicMock()
    workspace_sync_svc.fetch_all.return_value = fetch_report

    cli_output_svc = MagicMock()
    cli_output_svc.style.side_effect = lambda text, _style: text

    reporter_factory = MagicMock()
    drift_warning_svc = MagicMock()

    return WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=workspace_sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=drift_warning_svc,
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
    )


@pytest.fixture
def fetch_params() -> EnvFetchParams:
    return EnvFetchParams(patterns=[], scope=RepoScope.project, output_json=False)


def test_fetch_failed_with_empty_results_exits_nonzero_in_text_mode(
    fetch_params: EnvFetchParams,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """Regression: a failed fetch whose project/standalone lists end up empty
    must exit non-zero in text mode (the JSON branch already did).

    This shape arises when every requested worktree is dropped by the
    workspace service (e.g. all missing on disk) but the run is still a
    failure. Previously the handler hit the "Nothing to fetch" early return
    and exited 0.
    """
    # Force success=False without populating either list. FetchReport.success
    # can't otherwise be False with empty lists, but the handler shape must
    # still fail-closed if future code paths produce that combination.
    report = FetchReport(projects=[], standalone=[])
    handler = _make_handler(report)

    with patch.object(FetchReport, "success", new_callable=PropertyMock) as success:
        success.return_value = False
        with pytest.raises(SystemExit) as excinfo:
            handler.fetch(fetch_params)

    assert excinfo.value.code == 1
    # "Nothing to fetch" must not be emitted on a failed run.
    assert "Nothing to fetch" not in capsys.readouterr().out


def test_fetch_succeeded_with_empty_results_exits_zero_with_message(
    fetch_params: EnvFetchParams,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """Happy path: empty + success is the genuine 'nothing to fetch' case."""
    handler = _make_handler(FetchReport(projects=[], standalone=[]))

    handler.fetch(fetch_params)  # no SystemExit

    assert "Nothing to fetch" in capsys.readouterr().out


def test_fetch_failed_with_populated_results_exits_nonzero(
    fetch_params: EnvFetchParams,
) -> None:
    """Sanity: a failure with non-empty results still exits non-zero."""
    handler = _make_handler(
        FetchReport(
            projects=[RepoFetchOutcome(repo_name="demo", success=False, error="boom")],
            standalone=[],
        )
    )

    with pytest.raises(SystemExit) as excinfo:
        handler.fetch(fetch_params)

    assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# worktrees()
# ---------------------------------------------------------------------------


def _make_worktree_mock(env: Any, repo: Any) -> MagicMock:
    """Build a mock FeatureWorktree with the attributes worktrees() reads."""
    wt = MagicMock()
    wt.path = env.path / repo.name
    wt.environment.name = env.name
    wt.repository.name = repo.name
    return wt


def _make_env_worktrees_mock(env: Any, repos: list[Any]) -> MagicMock:
    """Build a mock FeatureEnvironmentWorktrees for one environment."""
    env_worktrees = MagicMock()
    env_worktrees.environment.name = env.name
    env_worktrees.worktrees = [_make_worktree_mock(env, repo) for repo in repos]
    return env_worktrees


def _make_worktrees_handler(
    environments: list[Any],
    project_repos: list[Any],
    standalone_repos: list[Any],
    workspace_repo_singleton: Any = None,
    workspace_status: Any = None,
) -> WorkspaceHandler:
    """Build a WorkspaceHandler with the minimum stubs worktrees() touches.

    `workspace_repo_singleton` stubs `RepositoryFactory.get_workspace_repo()`
    (the implicit workspace-root entry); it defaults to None so tests that
    don't care about the workspace row see no extra entry.
    """
    workspace_repo = MagicMock()
    workspace_repo.get_environments.return_value = environments

    repo_factory = MagicMock()
    repo_factory.get_project_repos.return_value = project_repos
    repo_factory.get_standalone_repos.return_value = standalone_repos
    repo_factory.get_workspace_repo.return_value = workspace_repo_singleton

    repo_repo = MagicMock()
    repo_repo.get_standalone_status.return_value = workspace_status

    env_status_svc = MagicMock()
    env_status_svc.get_feature_environment_worktrees.side_effect = lambda env, repos: _make_env_worktrees_mock(
        env, repos
    )

    cli_output_svc = MagicMock()
    cli_output_svc.render_table.return_value = []

    return WorkspaceHandler(
        env_status_svc=env_status_svc,
        workspace_sync_svc=MagicMock(),
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=workspace_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=MagicMock(),
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
    )


def test_worktrees_json_emits_expected_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """worktrees(output_json=True) emits the expected flat JSON array."""
    # Set up a real directory for the worktree so the exists() check passes.
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    # Set up a real directory for the standalone repo.
    standalone_dir = tmp_path / ".winter" / "ext" / "harness"
    standalone_dir.mkdir(parents=True)

    # Build mock FeatureEnvironment.
    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    # Build mock ProjectRepository.
    winter_repo = MagicMock()
    winter_repo.name = "winter"

    # Build mock StandaloneRepository.
    harness_standalone = MagicMock()
    harness_standalone.name = "winter-harness"
    harness_standalone.path = standalone_dir

    handler = _make_worktrees_handler(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[harness_standalone],
    )

    handler.worktrees(EnvWorktreesParams(output_json=True))

    out = capsys.readouterr().out
    items = json.loads(out)

    assert len(items) == 2

    wt_item = items[0]
    assert wt_item["kind"] == "worktree"
    assert wt_item["env"] == "alpha"
    assert wt_item["repo"] == "winter"
    assert wt_item["name"] is None
    assert wt_item["label"] == "alpha/winter"
    assert wt_item["path"] == str(wt_dir)

    st_item = items[1]
    assert st_item["kind"] == "standalone"
    assert st_item["env"] is None
    assert st_item["repo"] is None
    assert st_item["name"] == "winter-harness"
    assert st_item["label"] == "winter-harness"
    assert st_item["path"] == str(standalone_dir)


def test_worktrees_omits_nonexistent_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """Entries whose directory does not exist on disk are excluded."""
    # Neither the worktree dir nor the standalone dir exist.
    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    harness_standalone = MagicMock()
    harness_standalone.name = "winter-harness"
    harness_standalone.path = tmp_path / ".winter" / "ext" / "harness"

    handler = _make_worktrees_handler(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[harness_standalone],
    )

    handler.worktrees(EnvWorktreesParams(output_json=True))

    out = capsys.readouterr().out
    items = json.loads(out)

    assert items == []


def test_worktrees_mixed_existence_filters_per_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """Only worktrees whose directory exists on disk appear in output.

    Two envs, two repos each. Only one env's worktree dir is created — the
    other env's dir is absent. Proves per-entry filtering, not all-or-nothing.
    """
    # alpha/winter exists; beta/winter does not.
    alpha_wt_dir = tmp_path / "alpha" / "winter"
    alpha_wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    beta_env = MagicMock()
    beta_env.name = "beta"
    beta_env.path = tmp_path / "beta"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    handler = _make_worktrees_handler(
        environments=[alpha_env, beta_env],
        project_repos=[winter_repo],
        standalone_repos=[],
    )

    handler.worktrees(EnvWorktreesParams(output_json=True))

    out = capsys.readouterr().out
    items = json.loads(out)

    assert len(items) == 1
    assert items[0]["env"] == "alpha"
    assert items[0]["repo"] == "winter"
    assert items[0]["path"] == str(alpha_wt_dir)


def test_worktrees_human_table_calls_render_table_with_expected_rows(
    tmp_path: Path,
) -> None:
    """worktrees(output_json=False) passes the expected rows to render_table."""
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    # Hold a reference to the cli_output_svc mock so we can assert on it
    # without going through handler._cli_output_svc (which pyright types as
    # ICliOutputService and doesn't know about MagicMock attributes).
    cli_output_svc: MagicMock = MagicMock()
    cli_output_svc.render_table.return_value = []

    env_status_svc: MagicMock = MagicMock()
    env_status_svc.get_feature_environment_worktrees.side_effect = lambda env, repos: _make_env_worktrees_mock(
        env, repos
    )

    workspace_repo: MagicMock = MagicMock()
    workspace_repo.get_environments.return_value = [alpha_env]

    repo_factory: MagicMock = MagicMock()
    repo_factory.get_project_repos.return_value = [winter_repo]
    repo_factory.get_standalone_repos.return_value = []
    repo_factory.get_workspace_repo.return_value = None

    handler = WorkspaceHandler(
        env_status_svc=env_status_svc,
        workspace_sync_svc=MagicMock(),
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=workspace_repo,
        repo_repo=MagicMock(),
        repo_factory=repo_factory,
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=MagicMock(),
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
    )

    handler.worktrees(EnvWorktreesParams(output_json=False))

    cli_output_svc.render_table.assert_called_once()
    call_args = cli_output_svc.render_table.call_args
    rows = call_args[0][0]

    assert len(rows) == 1
    assert rows[0] == ["alpha/winter", "worktree", str(wt_dir)]


# ---------------------------------------------------------------------------
# worktrees() --status
# ---------------------------------------------------------------------------


def _make_worktree_repo_status(wt: Any, ahead: int, behind: int, dirty_count: int) -> WorktreeRepoStatus:
    """Build a WorktreeRepoStatus stub for use in --status tests."""
    return WorktreeRepoStatus(
        worktree=wt,
        branch="alpha",
        ahead=ahead,
        behind=behind,
        dirty_count=dirty_count,
    )


def _make_worktrees_handler_with_status(
    environments: list[Any],
    project_repos: list[Any],
    standalone_repos: list[Any],
    repo_statuses_by_env: dict[str, list[WorktreeRepoStatus]],
) -> WorkspaceHandler:
    """Build a WorkspaceHandler that returns per-env repo statuses for --status tests."""
    workspace_repo = MagicMock()
    workspace_repo.get_environments.return_value = environments

    repo_factory = MagicMock()
    repo_factory.get_project_repos.return_value = project_repos
    repo_factory.get_standalone_repos.return_value = standalone_repos
    repo_factory.get_workspace_repo.return_value = None

    def _make_env_worktrees(env: Any, repos: list[Any]) -> Any:
        return _make_env_worktrees_mock(env, repos)

    def _get_repo_statuses(env_worktrees: Any) -> list[WorktreeRepoStatus]:
        env_name = env_worktrees.environment.name
        return repo_statuses_by_env.get(env_name, [])

    env_status_svc = MagicMock()
    env_status_svc.get_feature_environment_worktrees.side_effect = _make_env_worktrees
    env_status_svc.get_worktree_repo_statuses.side_effect = _get_repo_statuses

    cli_output_svc = MagicMock()
    cli_output_svc.render_table.return_value = []

    return WorkspaceHandler(
        env_status_svc=env_status_svc,
        workspace_sync_svc=MagicMock(),
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=workspace_repo,
        repo_repo=MagicMock(),
        repo_factory=repo_factory,
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=MagicMock(),
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
    )


def test_worktrees_json_without_status_has_no_status_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """Without --status the three keys (ahead/behind/dirty) are absent from JSON."""
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    handler = _make_worktrees_handler(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[],
    )

    handler.worktrees(EnvWorktreesParams(output_json=True, with_status=False))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    item = items[0]
    assert "ahead" not in item
    assert "behind" not in item
    assert "dirty" not in item


def test_worktrees_json_with_status_includes_status_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """With --status, each worktree entry carries ahead/behind/dirty with correct values."""
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    # Build the worktree mock so we can attach a WorktreeRepoStatus to it.
    wt_mock = _make_worktree_mock(alpha_env, winter_repo)
    repo_status = _make_worktree_repo_status(wt_mock, ahead=3, behind=1, dirty_count=2)

    handler = _make_worktrees_handler_with_status(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[],
        repo_statuses_by_env={"alpha": [repo_status]},
    )

    handler.worktrees(EnvWorktreesParams(output_json=True, with_status=True))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    item = items[0]
    assert item["ahead"] == 3
    assert item["behind"] == 1
    assert item["dirty"] == 2


def test_worktrees_json_with_status_clean_repo_has_zero_dirty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """A clean repo yields dirty=0 (and ahead=0, behind=0) with --status."""
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    wt_mock = _make_worktree_mock(alpha_env, winter_repo)
    repo_status = _make_worktree_repo_status(wt_mock, ahead=0, behind=0, dirty_count=0)

    handler = _make_worktrees_handler_with_status(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[],
        repo_statuses_by_env={"alpha": [repo_status]},
    )

    handler.worktrees(EnvWorktreesParams(output_json=True, with_status=True))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    item = items[0]
    assert item["ahead"] == 0
    assert item["behind"] == 0
    assert item["dirty"] == 0


# ---------------------------------------------------------------------------
# worktrees() — implicit workspace repo entry
# ---------------------------------------------------------------------------


def test_worktrees_json_includes_workspace_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """The implicit workspace repo appears as a `<workspace>` entry in --json output."""
    wt_dir = tmp_path / "alpha" / "winter"
    wt_dir.mkdir(parents=True)

    alpha_env = MagicMock()
    alpha_env.name = "alpha"
    alpha_env.path = tmp_path / "alpha"

    winter_repo = MagicMock()
    winter_repo.name = "winter"

    workspace_singleton = StandaloneRepository(name="winter-workspace", path=tmp_path)

    handler = _make_worktrees_handler(
        environments=[alpha_env],
        project_repos=[winter_repo],
        standalone_repos=[],
        workspace_repo_singleton=workspace_singleton,
    )

    handler.worktrees(EnvWorktreesParams(output_json=True))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 2

    ws_item = next(i for i in items if i["kind"] == "workspace")
    assert ws_item["env"] is None
    assert ws_item["repo"] is None
    assert ws_item["name"] == "winter-workspace"
    assert ws_item["label"] == "<workspace>"
    assert ws_item["path"] == str(tmp_path)


def test_worktrees_omits_workspace_entry_when_absent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """No workspace entry is emitted when get_workspace_repo() returns None."""
    handler = _make_worktrees_handler(
        environments=[],
        project_repos=[],
        standalone_repos=[],
        workspace_repo_singleton=None,
    )

    handler.worktrees(EnvWorktreesParams(output_json=True))

    items = json.loads(capsys.readouterr().out)
    assert items == []


def test_worktrees_human_table_includes_workspace_row(
    tmp_path: Path,
) -> None:
    """The workspace entry surfaces as a `<workspace>` row in the human table."""
    workspace_singleton = StandaloneRepository(name="winter-workspace", path=tmp_path)

    handler = _make_worktrees_handler(
        environments=[],
        project_repos=[],
        standalone_repos=[],
        workspace_repo_singleton=workspace_singleton,
    )

    cli_output_svc = cast(MagicMock, handler._cli_output_svc)
    handler.worktrees(EnvWorktreesParams(output_json=False))

    cli_output_svc.render_table.assert_called_once()
    rows = cli_output_svc.render_table.call_args[0][0]
    assert rows == [["<workspace>", "workspace", str(tmp_path)]]


def test_worktrees_with_status_populates_workspace_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """--status derives ahead/behind/dirty for the workspace entry from its repo status."""
    workspace_singleton = StandaloneRepository(name="winter-workspace", path=tmp_path)
    workspace_status = StandaloneRepoStatus(
        repository=workspace_singleton,
        branch="workspace",
        ahead=2,
        behind=1,
        dirty_count=4,
    )

    handler = _make_worktrees_handler(
        environments=[],
        project_repos=[],
        standalone_repos=[],
        workspace_repo_singleton=workspace_singleton,
        workspace_status=workspace_status,
    )

    handler.worktrees(EnvWorktreesParams(output_json=True, with_status=True))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    ws_item = items[0]
    assert ws_item["kind"] == "workspace"
    assert ws_item["ahead"] == 2
    assert ws_item["behind"] == 1
    assert ws_item["dirty"] == 4


def test_worktrees_with_status_workspace_status_none_when_branch_absent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[Any],
) -> None:
    """When the workspace status has no active branch, status fields fall back to None."""
    workspace_singleton = StandaloneRepository(name="winter-workspace", path=tmp_path)
    # branch=None models a missing / not-a-repo workspace root — status undecidable.
    workspace_status = StandaloneRepoStatus(repository=workspace_singleton, branch=None)

    handler = _make_worktrees_handler(
        environments=[],
        project_repos=[],
        standalone_repos=[],
        workspace_repo_singleton=workspace_singleton,
        workspace_status=workspace_status,
    )

    handler.worktrees(EnvWorktreesParams(output_json=True, with_status=True))

    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    ws_item = items[0]
    assert ws_item["ahead"] is None
    assert ws_item["behind"] is None
    assert ws_item["dirty"] is None


# ---------------------------------------------------------------------------
# compute_status_exit_code — pure helper
# ---------------------------------------------------------------------------


def _clean_wt_snapshot(repo: str = "repo-a") -> WorktreeSnapshot:
    return WorktreeSnapshot(
        repo=repo,
        branch="alpha",
        upstream="origin/feature/x",
        ahead=0,
        behind=0,
        tracking_ahead=0,
        tracking_behind=0,
        tracking_ref_present=True,
        staged=0,
        unstaged=0,
        untracked=0,
        dirty=0,
        last_commit_subject=None,
    )


def _dirty_wt_snapshot(repo: str = "repo-a", dirty: int = 1) -> WorktreeSnapshot:
    return WorktreeSnapshot(
        repo=repo,
        branch="alpha",
        upstream="origin/feature/x",
        ahead=0,
        behind=0,
        tracking_ahead=0,
        tracking_behind=0,
        tracking_ref_present=True,
        staged=1,
        unstaged=0,
        untracked=0,
        dirty=dirty,
        last_commit_subject=None,
    )


def _clean_env_snapshot(name: str = "alpha", worktrees: list[WorktreeSnapshot] | None = None) -> EnvSnapshot:
    return EnvSnapshot(
        name=name,
        index=1,
        port_base=4100,
        feature_branch="feature/x",
        worktrees=worktrees if worktrees is not None else [_clean_wt_snapshot()],
    )


def _clean_sc_snapshot(repo: str = "repo-a") -> SourceCheckoutSnapshot:
    return SourceCheckoutSnapshot(
        repo=repo,
        branch="main",
        behind_origin=0,
        ahead_origin=0,
        dirty=0,
        drift=[],
    )


def _drifted_sc_snapshot(repo: str = "repo-a", behind: int = 3) -> SourceCheckoutSnapshot:
    return SourceCheckoutSnapshot(
        repo=repo,
        branch="main",
        behind_origin=behind,
        ahead_origin=0,
        dirty=0,
        drift=[],
    )


def _clean_workspace_level(root: str = "/ws") -> WorkspaceLevelSnapshot:
    return WorkspaceLevelSnapshot(
        root_path=root,
        extensions=[],
        orphans=[],
        drift_missing=[],
        drift_undeclared=[],
    )


def _make_snapshot(
    envs: list[EnvSnapshot] | None = None,
    source_checkouts: list[SourceCheckoutSnapshot] | None = None,
    workspace: WorkspaceLevelSnapshot | None = None,
    dashboard: DashboardSnapshot | None = None,
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        schema_version=1,
        environments=envs if envs is not None else [_clean_env_snapshot()],
        source_checkouts=source_checkouts if source_checkouts is not None else [_clean_sc_snapshot()],
        workspace=workspace if workspace is not None else _clean_workspace_level(),
        dashboard=dashboard
        if dashboard is not None
        else DashboardSnapshot(configured_layout="auto", resolved_layout="repos-as-rows"),
    )


def test_compute_status_exit_code_clean_snapshot_returns_zero() -> None:
    snapshot = _make_snapshot()
    assert compute_status_exit_code(snapshot, scoped=False) == 0


def test_compute_status_exit_code_dirty_worktree_returns_one() -> None:
    env = _clean_env_snapshot(worktrees=[_dirty_wt_snapshot(dirty=2)])
    snapshot = _make_snapshot(envs=[env])
    assert compute_status_exit_code(snapshot, scoped=False) == 1


def test_compute_status_exit_code_drifted_source_checkout_returns_one() -> None:
    snapshot = _make_snapshot(
        envs=[_clean_env_snapshot()],
        source_checkouts=[_drifted_sc_snapshot(behind=3)],
    )
    assert compute_status_exit_code(snapshot, scoped=False) == 1


def test_compute_status_exit_code_scoped_clean_env_global_drift_returns_zero() -> None:
    """Pattern scoping: clean env + global drift → exit 0 (drift ignored for scoped run)."""
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(
        envs=[env],
        source_checkouts=[_drifted_sc_snapshot(behind=5)],
    )
    assert compute_status_exit_code(snapshot, scoped=True) == 0


def test_compute_status_exit_code_scoped_dirty_env_returns_one() -> None:
    """Pattern scoping: dirty worktree in filtered snapshot → exit 1."""
    env = _clean_env_snapshot(name="alpha", worktrees=[_dirty_wt_snapshot(dirty=3)])
    snapshot = _make_snapshot(envs=[env])
    assert compute_status_exit_code(snapshot, scoped=True) == 1


def test_compute_status_exit_code_scoped_ignores_orphans() -> None:
    """Pattern scoping: orphans in workspace level do not flip exit code."""
    orphan = OrphanSnapshot(kind="project_clone", path="/ws/projects/ghost", safe_to_remove=True, notes="")
    ws = WorkspaceLevelSnapshot(
        root_path="/ws",
        extensions=[],
        orphans=[orphan],
        drift_missing=[],
        drift_undeclared=[],
    )
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(envs=[env], workspace=ws)
    assert compute_status_exit_code(snapshot, scoped=True) == 0


def test_compute_status_exit_code_orphans_return_one_when_unscoped() -> None:
    orphan = OrphanSnapshot(kind="project_clone", path="/ws/projects/ghost", safe_to_remove=True, notes="")
    ws = WorkspaceLevelSnapshot(
        root_path="/ws",
        extensions=[],
        orphans=[orphan],
        drift_missing=[],
        drift_undeclared=[],
    )
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(envs=[env], workspace=ws)
    assert compute_status_exit_code(snapshot, scoped=False) == 1


# ---------------------------------------------------------------------------
# status() handler integration — JSON shape + exit codes
# ---------------------------------------------------------------------------


def _make_status_handler(snapshot: WorkspaceSnapshot | None, raise_exc: Exception | None = None) -> WorkspaceHandler:
    """Build a WorkspaceHandler wired with a stub WorkspaceSnapshotService."""
    workspace_snapshot_svc = MagicMock()
    if raise_exc is not None:
        workspace_snapshot_svc.collect.side_effect = raise_exc
    else:
        workspace_snapshot_svc.collect.return_value = snapshot

    cli_output_svc = MagicMock()
    cli_output_svc.style.side_effect = lambda text, _style: text
    cli_output_svc.render_table.return_value = []

    return WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=MagicMock(),
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=MagicMock(),
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
        workspace_snapshot_svc=workspace_snapshot_svc,
    )


def test_status_json_clean_snapshot_exits_zero(capsys: pytest.CaptureFixture[Any]) -> None:
    snapshot = _make_snapshot()
    handler = _make_status_handler(snapshot)
    handler.status(EnvStatusParams(patterns=[], output_json=True))  # no SystemExit
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1


def test_status_json_emits_all_top_level_keys(capsys: pytest.CaptureFixture[Any]) -> None:
    snapshot = _make_snapshot()
    handler = _make_status_handler(snapshot)
    handler.status(EnvStatusParams(patterns=[], output_json=True))
    out = json.loads(capsys.readouterr().out)
    assert "schema_version" in out
    assert "environments" in out
    assert "source_checkouts" in out
    assert "workspace" in out
    assert "dashboard" in out


def test_status_json_passes_through_dashboard_block(capsys: pytest.CaptureFixture[Any]) -> None:
    """The snapshot's dashboard block surfaces verbatim in --json output."""
    snapshot = _make_snapshot(
        dashboard=DashboardSnapshot(configured_layout="auto", resolved_layout="list"),
    )
    handler = _make_status_handler(snapshot)
    handler.status(EnvStatusParams(patterns=[], output_json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["dashboard"] == {"configured_layout": "auto", "resolved_layout": "list"}


def test_status_json_emits_nested_worktrees(capsys: pytest.CaptureFixture[Any]) -> None:
    snapshot = _make_snapshot(
        envs=[_clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot("repo-a")])],
    )
    handler = _make_status_handler(snapshot)
    handler.status(EnvStatusParams(patterns=[], output_json=True))
    out = json.loads(capsys.readouterr().out)
    assert len(out["environments"]) == 1
    assert len(out["environments"][0]["worktrees"]) == 1
    assert out["environments"][0]["worktrees"][0]["repo"] == "repo-a"


def test_status_dirty_worktree_exits_one() -> None:
    env = _clean_env_snapshot(worktrees=[_dirty_wt_snapshot(dirty=1)])
    snapshot = _make_snapshot(envs=[env])
    handler = _make_status_handler(snapshot)
    with pytest.raises(SystemExit) as excinfo:
        handler.status(EnvStatusParams(patterns=[], output_json=False))
    assert excinfo.value.code == 1


def test_status_clean_env_global_drift_scoped_exits_zero(capsys: pytest.CaptureFixture[Any]) -> None:
    """Pattern-scoped run on clean env: exits 0 even with global source-checkout drift."""
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(
        envs=[env],
        source_checkouts=[_drifted_sc_snapshot(behind=3)],
    )
    handler = _make_status_handler(snapshot)
    handler.status(EnvStatusParams(patterns=["alpha"], output_json=False))  # no SystemExit
    capsys.readouterr()  # consume output


def test_status_command_error_exits_two() -> None:
    """A ClickException from collect() maps to exit code 2."""
    handler = _make_status_handler(None, raise_exc=click.ClickException("No worktrees match: nope"))
    with pytest.raises(SystemExit) as excinfo:
        handler.status(EnvStatusParams(patterns=["nope"], output_json=False))
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# status() --fetch flag
# ---------------------------------------------------------------------------


def _make_status_handler_with_sync(
    snapshot: WorkspaceSnapshot,
    fetch_report: FetchReport | None = None,
) -> tuple[WorkspaceHandler, MagicMock]:
    """Build a WorkspaceHandler with both snapshot and sync service stubs.

    Returns the handler and the workspace_sync_svc mock so tests can assert
    fetch_all call counts and arguments.
    """
    workspace_snapshot_svc = MagicMock()
    workspace_snapshot_svc.collect.return_value = snapshot

    workspace_sync_svc = MagicMock()
    if fetch_report is None:
        fetch_report = FetchReport(projects=[], standalone=[])
    workspace_sync_svc.fetch_all.return_value = fetch_report

    reporter_factory = MagicMock()

    cli_output_svc = MagicMock()
    cli_output_svc.style.side_effect = lambda text, _style: text
    cli_output_svc.render_table.return_value = []

    handler = WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=workspace_sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
        workspace_snapshot_svc=workspace_snapshot_svc,
    )
    return handler, workspace_sync_svc


def test_status_no_fetch_by_default_does_not_call_sync_service() -> None:
    """Default (no --fetch): the sync service must not be invoked (no network)."""
    snapshot = _make_snapshot()
    handler, sync_svc = _make_status_handler_with_sync(snapshot)

    handler.status(EnvStatusParams(patterns=[], output_json=False))

    sync_svc.fetch_all.assert_not_called()


def test_status_with_fetch_flag_calls_fetch_all_before_collect(capsys: pytest.CaptureFixture[Any]) -> None:
    """--fetch: sync service is invoked before snapshot collection."""
    snapshot = _make_snapshot()
    handler, sync_svc = _make_status_handler_with_sync(snapshot)

    call_order: list[str] = []
    sync_svc.fetch_all.side_effect = lambda **kwargs: (
        call_order.append("fetch") or FetchReport(projects=[], standalone=[])
    )
    ws_snap_svc = cast(MagicMock, handler._workspace_snapshot_svc)
    ws_snap_svc.collect.side_effect = lambda **kwargs: call_order.append("collect") or snapshot

    handler.status(EnvStatusParams(patterns=[], output_json=False, fetch=True))
    capsys.readouterr()

    assert call_order == ["fetch", "collect"]


def test_status_with_fetch_unscoped_passes_empty_patterns(capsys: pytest.CaptureFixture[Any]) -> None:
    """--fetch without patterns passes patterns=[] (fetch_all expands to */* internally)."""
    snapshot = _make_snapshot()
    handler, sync_svc = _make_status_handler_with_sync(snapshot)

    handler.status(EnvStatusParams(patterns=[], output_json=False, fetch=True))
    capsys.readouterr()

    sync_svc.fetch_all.assert_called_once()
    _, kwargs = sync_svc.fetch_all.call_args
    assert kwargs["patterns"] == []
    assert kwargs["scope"] == RepoScope.project


def test_status_with_fetch_scoped_passes_patterns_to_fetch(capsys: pytest.CaptureFixture[Any]) -> None:
    """--fetch with patterns passes those patterns to scope the fetch."""
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(envs=[env])
    handler, sync_svc = _make_status_handler_with_sync(snapshot)

    handler.status(EnvStatusParams(patterns=["alpha"], output_json=False, fetch=True))
    capsys.readouterr()

    sync_svc.fetch_all.assert_called_once()
    _, kwargs = sync_svc.fetch_all.call_args
    assert kwargs["patterns"] == ["alpha"]
    assert kwargs["scope"] == RepoScope.project


# ---------------------------------------------------------------------------
# compute_status_exit_code — dirty source checkout (fix 2)
# ---------------------------------------------------------------------------


def _dirty_sc_snapshot(repo: str = "repo-a", dirty: int = 1) -> SourceCheckoutSnapshot:
    return SourceCheckoutSnapshot(
        repo=repo,
        branch="main",
        behind_origin=0,
        ahead_origin=0,
        dirty=dirty,
        drift=[],
    )


def test_compute_status_exit_code_dirty_source_checkout_returns_one() -> None:
    """A source checkout with dirty files (but in-sync with origin) returns exit 1."""
    snapshot = _make_snapshot(
        envs=[_clean_env_snapshot()],
        source_checkouts=[_dirty_sc_snapshot(dirty=3)],
    )
    assert compute_status_exit_code(snapshot, scoped=False) == 1


def test_compute_status_exit_code_dirty_source_checkout_scoped_returns_zero() -> None:
    """Pattern scoping: dirty source checkout does NOT flip exit code for a scoped run."""
    env = _clean_env_snapshot(name="alpha", worktrees=[_clean_wt_snapshot()])
    snapshot = _make_snapshot(
        envs=[env],
        source_checkouts=[_dirty_sc_snapshot(dirty=2)],
    )
    assert compute_status_exit_code(snapshot, scoped=True) == 0


# ---------------------------------------------------------------------------
# status() JSON wire-shape test — schema v1 contract including pinned (fix 6)
# ---------------------------------------------------------------------------


def _pinned_wt_snapshot(repo: str = "repo-a") -> WorktreeSnapshot:
    return WorktreeSnapshot(
        repo=repo,
        branch="alpha",
        upstream="origin/feature/x",
        ahead=2,
        behind=1,
        tracking_ahead=2,
        tracking_behind=1,
        tracking_ref_present=True,
        staged=1,
        unstaged=2,
        untracked=3,
        dirty=6,
        last_commit_subject="feat: add thing",
        pinned=True,
        main_branch="master",
    )


def test_status_json_emits_v1_schema(capsys: pytest.CaptureFixture[Any]) -> None:
    """JSON output satisfies the v1 schema contract: top-level keys, worktrees[] includes pinned.

    The snapshot has a dirty worktree so compute_status_exit_code returns 1; the
    JSON is emitted before sys.exit, so we catch the SystemExit and inspect the
    already-captured stdout.
    """
    env = _clean_env_snapshot(name="alpha", worktrees=[_pinned_wt_snapshot("repo-a")])
    snapshot = _make_snapshot(envs=[env])
    handler = _make_status_handler(snapshot)

    with pytest.raises(SystemExit) as excinfo:
        handler.status(EnvStatusParams(patterns=[], output_json=True))
    assert excinfo.value.code == 1  # dirty worktree → exit 1 (JSON still emitted)

    out = json.loads(capsys.readouterr().out)

    # top-level keys
    assert out["schema_version"] == 1
    assert "environments" in out
    assert "source_checkouts" in out
    assert "workspace" in out

    # worktrees[] entry — every documented field including pinned
    wt = out["environments"][0]["worktrees"][0]
    assert wt["repo"] == "repo-a"
    assert wt["branch"] == "alpha"
    assert wt["upstream"] == "origin/feature/x"
    assert wt["ahead"] == 2
    assert wt["behind"] == 1
    assert wt["tracking_ahead"] == 2
    assert wt["tracking_behind"] == 1
    assert wt["tracking_ref_present"] is True
    assert wt["staged"] == 1
    assert wt["unstaged"] == 2
    assert wt["untracked"] == 3
    assert wt["dirty"] == 6
    assert wt["last_commit_subject"] == "feat: add thing"
    assert wt["pinned"] is True
    assert wt["main_branch"] == "master"


# ---------------------------------------------------------------------------
# status() JSON — extensions key present on env (Fix 5)
# ---------------------------------------------------------------------------


def test_status_json_env_includes_extensions_key(capsys: pytest.CaptureFixture[Any]) -> None:
    """The `extensions` key is always present on each env in --json output.

    An EnvSnapshot with a non-empty extensions dict surfaces it verbatim; an
    env with no decorators emits an empty dict.
    """
    env_with_badge = EnvSnapshot(
        name="alpha",
        index=1,
        port_base=4020,
        feature_branch="feature/x",
        worktrees=[_clean_wt_snapshot()],
        extensions={"wst": "● 3/3"},
    )
    env_empty = EnvSnapshot(
        name="beta",
        index=2,
        port_base=4040,
        feature_branch=None,
        worktrees=[],
        extensions={},
    )
    snapshot = _make_snapshot(envs=[env_with_badge, env_empty])
    handler = _make_status_handler(snapshot)

    handler.status(EnvStatusParams(patterns=[], output_json=True))

    out = json.loads(capsys.readouterr().out)
    assert "extensions" in out["environments"][0]
    assert out["environments"][0]["extensions"] == {"wst": "● 3/3"}
    assert "extensions" in out["environments"][1]
    assert out["environments"][1]["extensions"] == {}


# ---------------------------------------------------------------------------
# status() JSON — schema parity (Fix 2): live output satisfies ws-status-v1.json
# ---------------------------------------------------------------------------


def test_status_json_keys_match_ws_status_v1_schema(capsys: pytest.CaptureFixture[Any]) -> None:
    """ws status --json output keys must stay in sync with the checked-in schema.

    Stdlib-only parity guard (no third-party validator dependency): the schema's
    object defs are `additionalProperties: false`, so this is the root-cause fix
    for silent schema drift — any field serialized into `ws status --json` that is
    not declared in schemas/ws-status-v1.json (or any schema-`required` field
    dropped from the output) makes this test red, forcing schema and serializer to
    stay in sync.
    """
    schema_path = Path(__file__).parents[3] / "schemas" / "ws-status-v1.json"
    assert schema_path.exists(), f"Schema not found at {schema_path}"
    schema = json.loads(schema_path.read_text())
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def check(obj: dict[str, Any], def_name: str) -> None:
        d = defs[def_name]
        declared = set(d.get("properties", {}))
        required = set(d.get("required", []))
        keys = set(obj)
        assert not (keys - declared), f"{def_name}: serialized keys not declared in schema: {sorted(keys - declared)}"
        assert not (required - keys), f"{def_name}: schema-required keys missing from output: {sorted(required - keys)}"

    env = EnvSnapshot(
        name="alpha",
        index=1,
        port_base=4020,
        feature_branch="feature/my-branch",
        worktrees=[_pinned_wt_snapshot("repo-a")],
        extensions={"wst": "● 2/2"},
    )
    snapshot = _make_snapshot(envs=[env])
    handler = _make_status_handler(snapshot)

    with pytest.raises(SystemExit):
        handler.status(EnvStatusParams(patterns=[], output_json=True))

    doc = json.loads(capsys.readouterr().out)

    top_declared = set(schema.get("properties", {}))
    assert not (set(doc) - top_declared), f"top-level keys not declared in schema: {sorted(set(doc) - top_declared)}"

    for env_obj in doc["environments"]:
        check(env_obj, "EnvSnapshot")
        for wt in env_obj["worktrees"]:
            check(wt, "WorktreeSnapshot")
    for sc in doc.get("source_checkouts", []):
        check(sc, "SourceCheckoutSnapshot")
    check(doc["workspace"], "WorkspaceLevelSnapshot")
    for orphan in doc["workspace"].get("orphans", []):
        check(orphan, "OrphanSnapshot")
    for pin in doc["workspace"].get("standalone_pins", []):
        check(pin, "StandalonePinSnapshot")
    check(doc["dashboard"], "DashboardSnapshot")


# ── connect ──────────────────────────────────────────────────────────────────


def _connect_handler(connect_returns: dict[str, list[str]]) -> tuple[WorkspaceHandler, MagicMock]:
    """Handler whose connect_env returns `connect_returns[env_name]` per env.

    Returns the handler plus the workspace_repo mock so tests can assert which
    resolution path (get_environment by name vs. get_environments discovery)
    was taken.
    """
    from types import SimpleNamespace

    workspace_repo = MagicMock()
    workspace_repo.get_environment.side_effect = lambda _ws, name: SimpleNamespace(name=name)
    workspace_repo.get_environments.return_value = [
        SimpleNamespace(name="alpha"),
        SimpleNamespace(name="beta"),
    ]

    env_status_svc = MagicMock()
    env_status_svc.get_feature_environment_worktrees.side_effect = lambda env, _repos: env

    env_checkout_svc = MagicMock()
    env_checkout_svc.connect_env.side_effect = lambda env, _branch, _patterns: connect_returns.get(env.name, [])

    cli_output_svc = MagicMock()
    cli_output_svc.style.side_effect = lambda text, _style: text

    handler = WorkspaceHandler(
        env_status_svc=env_status_svc,
        workspace_sync_svc=MagicMock(),
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=env_checkout_svc,
        workspace_repo=workspace_repo,
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=MagicMock(),
        cli_output_svc=cli_output_svc,
        workspace=MagicMock(),
    )
    return handler, workspace_repo


def test_connect_literal_env_resolved_by_name_not_discovery() -> None:
    """A non-glob env pattern resolves by name — so non-Greek env names still connect."""
    from winter_cli.modules.workspace.handlers.workspace_handler import EnvConnectParams

    handler, workspace_repo = _connect_handler({"my-feature": ["api"]})
    handler.connect(EnvConnectParams(patterns=["my-feature/api"], feature_branch="feature/x", output_json=True))

    workspace_repo.get_environment.assert_called_once()
    assert workspace_repo.get_environment.call_args.args[1] == "my-feature"
    workspace_repo.get_environments.assert_not_called()


def test_connect_glob_env_uses_discovery() -> None:
    """A glob env segment falls back to discovering existing envs."""
    from winter_cli.modules.workspace.handlers.workspace_handler import EnvConnectParams

    handler, workspace_repo = _connect_handler({"alpha": ["api"], "beta": ["api"]})
    handler.connect(EnvConnectParams(patterns=["*/api"], feature_branch="feature/x", output_json=True))

    workspace_repo.get_environments.assert_called_once()


def test_connect_json_shape_lists_connected_worktrees(capsys: pytest.CaptureFixture[Any]) -> None:
    from winter_cli.modules.workspace.handlers.workspace_handler import EnvConnectParams

    handler, _ = _connect_handler({"alpha": ["api", "web"]})
    handler.connect(EnvConnectParams(patterns=["alpha"], feature_branch="feature/x", output_json=True))

    out = json.loads(capsys.readouterr().out)
    assert out["feature_branch"] == "feature/x"
    assert out["count"] == 2
    assert out["connected"] == [
        {"env": "alpha", "repo": "api"},
        {"env": "alpha", "repo": "web"},
    ]


def test_connect_no_match_reports_and_exits_zero(capsys: pytest.CaptureFixture[Any]) -> None:
    from winter_cli.modules.workspace.handlers.workspace_handler import EnvConnectParams

    handler, _ = _connect_handler({"alpha": []})
    handler.connect(EnvConnectParams(patterns=["alpha/nope"], feature_branch="feature/x", output_json=False))

    assert "No worktrees matched" in capsys.readouterr().out
