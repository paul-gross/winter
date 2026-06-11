from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.models import (
    CheckoutResult,
    FeatureEnvironment,
    FeatureEnvironmentWorktrees,
    FeatureWorktree,
    ProjectRepository,
    Workspace,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace() -> Workspace:
    return Workspace(root_path=WORKSPACE_ROOT, session_prefix="t", main_branch="main")


class FakeWriteRepoRepository:
    """Stub for the `IWriteRepoRepository` Protocol — records every call.

    Pre-seed before the call:
    - `dirty_worktree_repos` / `repos_with_commits_not_in`: repo-name sets; the
      matching query returns True/non-zero for repos in the set.
    - `missing_refs`: `(repo_name, ref)` pairs absent from the local store, so
      `has_local_ref` can model "feature ref missing but `origin/main` present".
    - `upstreams`: repo-name → current upstream (e.g. `origin/feature-123`), or
      absent (None) for a disconnected repo.

    `count_commits_not_in_calls` records the `(repo, ref)` each abandonment
    check compared against — the assertion that the guard uses the repo's *own*
    upstream rather than the target.
    """

    def __init__(self) -> None:
        self.set_upstream_calls: list[tuple[str, str]] = []
        self.set_push_default_calls: list[str] = []
        self.unset_upstream_calls: list[str] = []
        self.hard_reset_calls: list[tuple[str, str]] = []
        self.count_commits_not_in_calls: list[tuple[str, str]] = []
        self.missing_refs: set[tuple[str, str]] = set()
        self.dirty_worktree_repos: set[str] = set()
        self.repos_with_commits_not_in: set[str] = set()
        self.upstreams: dict[str, str] = {}

    def set_upstream(self, worktree: FeatureWorktree, upstream: str) -> None:
        self.set_upstream_calls.append((worktree.repository.name, upstream))

    def set_push_default(self, worktree: FeatureWorktree) -> None:
        self.set_push_default_calls.append(worktree.repository.name)

    def unset_upstream(self, worktree: FeatureWorktree) -> None:
        self.unset_upstream_calls.append(worktree.repository.name)

    def get_worktree_upstream(self, worktree: FeatureWorktree) -> str | None:
        return self.upstreams.get(worktree.repository.name)

    def has_local_ref(self, worktree: FeatureWorktree, ref: str) -> bool:
        return (worktree.repository.name, ref) not in self.missing_refs

    def is_worktree_dirty(self, worktree: FeatureWorktree) -> bool:
        return worktree.repository.name in self.dirty_worktree_repos

    def count_commits_not_in(self, worktree: FeatureWorktree, ref: str) -> int:
        self.count_commits_not_in_calls.append((worktree.repository.name, ref))
        return 1 if worktree.repository.name in self.repos_with_commits_not_in else 0

    def hard_reset(self, worktree: FeatureWorktree, ref: str) -> None:
        self.hard_reset_calls.append((worktree.repository.name, ref))

    # Methods touched by other EnvCheckoutService code paths — raise to surface
    # accidental fan-out beyond the call under test.
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


@pytest.fixture
def fake_repo_repo() -> FakeWriteRepoRepository:
    return FakeWriteRepoRepository()


@pytest.fixture
def service(fake_repo_repo: FakeWriteRepoRepository) -> EnvCheckoutService:
    return EnvCheckoutService(repo_repo=fake_repo_repo)  # type: ignore[arg-type]


def _env_worktrees(workspace: Workspace, repos: list[ProjectRepository]) -> FeatureEnvironmentWorktrees:
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=workspace.root_path / "alpha")
    worktrees = [FeatureWorktree(workspace=workspace, environment=env, repository=r) for r in repos]
    return FeatureEnvironmentWorktrees(environment=env, worktrees=worktrees)


def test_connect_env_sets_upstream_for_non_pinned(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """`connect_env` invokes `set_upstream(origin/<feature>) + set_push_default` per non-pinned worktree."""
    repos = [
        ProjectRepository(name="feature-repo", main_path=workspace.root_path / "feature-repo", main_branch="main"),
        ProjectRepository(
            name="pinned-repo", main_path=workspace.root_path / "pinned-repo", main_branch="main", pinned=True
        ),
    ]
    env_wts = _env_worktrees(workspace, repos)

    count = service.connect_env(env_wts, feature_branch="feature/widget")

    assert count == 1
    assert fake_repo_repo.set_upstream_calls == [("feature-repo", "origin/feature/widget")]
    assert fake_repo_repo.set_push_default_calls == ["feature-repo"]


def test_disconnect_env_skips_pinned_and_unsets_others(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    repos = [
        ProjectRepository(name="feature-repo", main_path=workspace.root_path / "feature-repo", main_branch="main"),
        ProjectRepository(
            name="pinned-repo", main_path=workspace.root_path / "pinned-repo", main_branch="main", pinned=True
        ),
    ]
    env_wts = _env_worktrees(workspace, repos)

    count = service.disconnect_env(env_wts)

    assert count == 1
    assert fake_repo_repo.unset_upstream_calls == ["feature-repo"]


def test_checkout_env_resets_clean_repos_with_present_ref(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """Phase 1 passes for all repos → Phase 2 wires upstream + hard-resets each to the feature ref."""
    repos = [
        ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main"),
        ProjectRepository(name="r2", main_path=workspace.root_path / "r2", main_branch="main"),
    ]
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature/widget", force=False)

    assert report.aborted is False
    assert [(o.repo_name, o.result) for o in report.repos] == [
        ("r1", CheckoutResult.reset_feature),
        ("r2", CheckoutResult.reset_feature),
    ]
    assert fake_repo_repo.hard_reset_calls == [("r1", "origin/feature/widget"), ("r2", "origin/feature/widget")]
    assert fake_repo_repo.set_upstream_calls == [
        ("r1", "origin/feature/widget"),
        ("r2", "origin/feature/widget"),
    ]


def test_checkout_env_connects_and_resets_to_main_when_feature_ref_missing(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """A not-yet-pushed feature branch: connect every repo anyway, reset to origin/<main>."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="trunk")]
    fake_repo_repo.missing_refs.add(("r1", "origin/feature/new"))
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature/new", force=False)

    assert report.aborted is False
    assert [(o.repo_name, o.result) for o in report.repos] == [("r1", CheckoutResult.reset_main)]
    # Connected to the feature branch even though its ref doesn't exist yet...
    assert fake_repo_repo.set_upstream_calls == [("r1", "origin/feature/new")]
    # ...but reset to the repo's main branch.
    assert fake_repo_repo.hard_reset_calls == [("r1", "origin/trunk")]


def test_checkout_env_refuses_abandonment_against_own_upstream(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """Unpushed commits on the branch we're moving away from → refused_abandonment, abort."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main")]
    fake_repo_repo.upstreams["r1"] = "origin/feature-123"
    fake_repo_repo.repos_with_commits_not_in.add("r1")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature-xyz", force=False)

    assert report.aborted is True
    assert [(o.repo_name, o.result) for o in report.repos] == [("r1", CheckoutResult.refused_abandonment)]
    # The abandonment check compared against the repo's OWN upstream, not the target.
    assert fake_repo_repo.count_commits_not_in_calls == [("r1", "origin/feature-123")]
    assert fake_repo_repo.hard_reset_calls == []
    assert fake_repo_repo.set_upstream_calls == []


def test_checkout_env_abandonment_falls_back_to_main_when_disconnected(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """No current upstream → compare against origin/<main> so local-only commits still refuse."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main")]
    # upstreams unseeded → get_worktree_upstream returns None.
    fake_repo_repo.repos_with_commits_not_in.add("r1")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature-xyz", force=False)

    assert report.aborted is True
    assert fake_repo_repo.count_commits_not_in_calls == [("r1", "origin/main")]


def test_checkout_env_abandonment_falls_back_to_main_when_upstream_ref_absent(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """Upstream configured but its ref isn't local (never pushed) → fall back to origin/<main>."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main")]
    fake_repo_repo.upstreams["r1"] = "origin/feature-123"
    fake_repo_repo.missing_refs.add(("r1", "origin/feature-123"))
    env_wts = _env_worktrees(workspace, repos)

    service.checkout_env(env_wts, feature_branch="feature-xyz", force=False)

    assert fake_repo_repo.count_commits_not_in_calls == [("r1", "origin/main")]


def test_checkout_env_dirty_takes_precedence_over_abandonment(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """A repo that is both dirty and abandoning reports refused_dirty (single reason per repo)."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main")]
    fake_repo_repo.dirty_worktree_repos.add("r1")
    fake_repo_repo.repos_with_commits_not_in.add("r1")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature-xyz", force=False)

    assert report.aborted is True
    assert [(o.repo_name, o.result) for o in report.repos] == [("r1", CheckoutResult.refused_dirty)]
    # Dirty short-circuits — the abandonment check never runs for this repo.
    assert fake_repo_repo.count_commits_not_in_calls == []


def test_checkout_env_all_pinned_env_is_a_clean_noop(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """No non-pinned targets → non-aborted report with no repos and no mutation."""
    repos = [
        ProjectRepository(name="pinned", main_path=workspace.root_path / "pinned", main_branch="main", pinned=True),
    ]
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature-xyz", force=False)

    assert report.aborted is False
    assert report.repos == []
    assert fake_repo_repo.hard_reset_calls == []
    assert fake_repo_repo.set_upstream_calls == []


def test_checkout_env_force_bypasses_abandonment(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """--force skips Phase 1 entirely: no abandonment check, Phase 2 runs for every repo."""
    repos = [ProjectRepository(name="r1", main_path=workspace.root_path / "r1", main_branch="main")]
    fake_repo_repo.upstreams["r1"] = "origin/feature-123"
    fake_repo_repo.repos_with_commits_not_in.add("r1")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature-xyz", force=True)

    assert report.aborted is False
    assert [(o.repo_name, o.result) for o in report.repos] == [("r1", CheckoutResult.reset_feature)]
    assert fake_repo_repo.count_commits_not_in_calls == []
    assert fake_repo_repo.hard_reset_calls == [("r1", "origin/feature-xyz")]


def test_checkout_env_aborts_whole_env_when_any_repo_is_dirty_without_force(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """One dirty repo + force=False → no `hard_reset` runs in ANY repo (all-or-nothing safety)."""
    repos = [
        ProjectRepository(name="clean-repo", main_path=workspace.root_path / "clean-repo", main_branch="main"),
        ProjectRepository(name="dirty-repo", main_path=workspace.root_path / "dirty-repo", main_branch="main"),
    ]
    fake_repo_repo.dirty_worktree_repos.add("dirty-repo")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature/widget", force=False)

    assert report.aborted is True
    refused = [o for o in report.repos if o.result == CheckoutResult.refused_dirty]
    assert [o.repo_name for o in refused] == ["dirty-repo"]
    # The clean repo is not in the refused list and Phase 2 never runs.
    assert fake_repo_repo.hard_reset_calls == []
    assert fake_repo_repo.set_upstream_calls == []


def test_checkout_env_with_force_resets_dirty_repos(
    workspace: Workspace, service: EnvCheckoutService, fake_repo_repo: FakeWriteRepoRepository
) -> None:
    """`force=True` skips both dirty and abandonment gates; Phase 2 runs for every repo."""
    repos = [
        ProjectRepository(name="dirty-repo", main_path=workspace.root_path / "dirty-repo", main_branch="main"),
    ]
    fake_repo_repo.dirty_worktree_repos.add("dirty-repo")
    env_wts = _env_worktrees(workspace, repos)

    report = service.checkout_env(env_wts, feature_branch="feature/widget", force=True)

    assert report.aborted is False
    assert [(o.repo_name, o.result) for o in report.repos] == [("dirty-repo", CheckoutResult.reset_feature)]
    assert fake_repo_repo.hard_reset_calls == [("dirty-repo", "origin/feature/widget")]
