from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from winter_cli.modules.workspace.handlers.workspace_handler import (
    EnvFetchParams,
    EnvWorktreesParams,
    WorkspaceHandler,
)
from winter_cli.modules.workspace.models import (
    FetchReport,
    RepoFetchOutcome,
    RepoScope,
    WorktreeRepoStatus,
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
) -> WorkspaceHandler:
    """Build a WorkspaceHandler with the minimum stubs worktrees() touches."""
    workspace_repo = MagicMock()
    workspace_repo.get_environments.return_value = environments

    repo_factory = MagicMock()
    repo_factory.get_project_repos.return_value = project_repos
    repo_factory.get_standalone_repos.return_value = standalone_repos

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
        repo_repo=MagicMock(),
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
