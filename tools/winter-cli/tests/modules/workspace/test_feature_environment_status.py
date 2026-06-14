from __future__ import annotations

from pathlib import Path

from winter_cli.modules.workspace.models import FeatureEnvironment, FeatureEnvironmentStatus, Workspace

_WS = Workspace(root_path=Path("/ws"), session_prefix="t", main_branch="main")


def _status(feature_branch: str | None, distinct: int) -> FeatureEnvironmentStatus:
    env = FeatureEnvironment(workspace=_WS, name="alpha", index=1, path=_WS.root_path / "alpha")
    return FeatureEnvironmentStatus(environment=env, feature_branch=feature_branch, distinct_remote_count=distinct)


def test_label_is_plain_branch_for_single_remote() -> None:
    assert _status("feature/x", distinct=1).feature_branch_label() == "feature/x"


def test_label_appends_plus_n_for_multi_remote() -> None:
    # 5 distinct remotes → primary + 4 others → `feature/x+4`.
    assert _status("feature/x", distinct=5).feature_branch_label() == "feature/x+4"
    assert _status("feature/x", distinct=2).feature_branch_label() == "feature/x+1"


def test_label_uses_disconnected_placeholder_when_no_primary() -> None:
    assert _status(None, distinct=0).feature_branch_label() == "—"
    assert _status(None, distinct=0).feature_branch_label(disconnected="disconnected") == "disconnected"


def test_label_placeholder_only_when_nothing_connected() -> None:
    # `feature_branch` is the first *connected* repo, so it's None only when no
    # non-pinned worktree is connected (distinct == 0) — then no misleading `+N`.
    assert _status(None, distinct=0).feature_branch_label() == "—"
