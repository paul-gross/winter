from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from winter_cli.modules.workspace.handlers.workspace_handler import (
    EnvFetchParams,
    WorkspaceHandler,
)
from winter_cli.modules.workspace.models import (
    FetchReport,
    RepoFetchOutcome,
    RepoScope,
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
