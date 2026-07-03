"""Phase 6 tests: `winter ws update [<repo>] [--autostash]`.

Covers:
  - tag pin re-resolved to a new SHA → checkout_detached + lock rewritten + re_pinned
  - branch pin → checkout_branch + lock rewritten
  - <repo> targeting → only named repo re-pinned; others untouched
  - bare update → all pinned standalones; non-pinned untouched
  - dirty + no --autostash → REFUSED (no checkout, no lock write, pin_error outcome)
  - dirty + --autostash → succeeds (stash_push / stash_pop called)
  - unresolvable ref → per-repo failure, fan-out continues
  - resolved == current + lock current → up_to_date, no lock churn
  - other repos' lock entries preserved
  - CLI layer: ws update --help shows command + --autostash
  - handler routes name argument + bare form correctly
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeConfigLockRepository, FakeGitRepository
from winter_cli.config.models import (
    AdoptExtensions,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.handlers.workspace_handler import (
    EnvUpdateParams,
    WorkspaceHandler,
)
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import (
    PullMode,
    RepoError,
    RepoSyncOutcome,
    StandaloneRepository,
    SyncResult,
    Workspace,
)
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService

WORKSPACE_ROOT = Path("/ws")
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40
SHA_OTHER = "c" * 40


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_workspace_config(
    workspace_root: Path,
    standalone_configs: list[StandaloneRepositoryConfig],
) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=workspace_root,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        standalone_repos=standalone_configs,
    )


class FakeReadWorkspaceRepository:
    def get_environments(self, workspace, project_repos):  # type: ignore[no-untyped-def]
        return []

    def get_environment_status(self, env, project_repos):  # type: ignore[no-untyped-def]
        return None


class FakeWriteRepoRepository:
    def __init__(self, integrate_result: SyncResult = SyncResult.up_to_date) -> None:
        self.fetched_standalones: list[str] = []
        self.integrated_standalones: list[str] = []
        self._integrate_result = integrate_result

    def sync_ff_only(self, repo: Any) -> int:
        return 0

    def fetch_standalone(self, repo: StandaloneRepository) -> None:
        self.fetched_standalones.append(repo.name)

    def integrate_standalone(self, repo: StandaloneRepository, mode: PullMode, autostash: bool) -> RepoSyncOutcome:
        self.integrated_standalones.append(repo.name)
        return RepoSyncOutcome(repo_name=repo.name, sync_result=self._integrate_result)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"FakeWriteRepoRepository.{name} called unexpectedly")


class _NullPullReporter:
    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None:
        return None

    def pull_completed(self, success: bool) -> None:
        return None


class _RecordingPullReporter:
    def __init__(self) -> None:
        self.synced: list[tuple[str, str, SyncResult, str]] = []
        self.completed_success: bool | None = None

    def pull_started(self) -> None:
        return None

    def env_skipped(self, env: str, reason: str) -> None:
        return None

    def repo_synced(
        self,
        scope_label: str,
        repo_name: str,
        result: SyncResult,
        commits: int,
        ahead: int,
        behind: int,
        pin_ref: str = "",
    ) -> None:
        self.synced.append((scope_label, repo_name, result, pin_ref))

    def pull_completed(self, success: bool) -> None:
        self.completed_success = success


def _make_update_service(
    workspace_root: Path,
    standalone_configs: list[StandaloneRepositoryConfig],
    repo_repo: FakeWriteRepoRepository,
    git_repo: FakeGitRepository,
    config_lock_repo: FakeConfigLockRepository,
) -> WorkspaceSyncService:
    config = _make_workspace_config(workspace_root, standalone_configs)
    fake_worktree_repo = FakeReadWorkspaceRepository()
    env_status_svc = EnvStatusService(
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
    )
    git_ops = GitOpsService(RepoErrorFactory(), sleep=lambda _: None, jitter=lambda: 0.0)
    workspace = Workspace(root_path=workspace_root, service_prefix="t", main_branch="main")
    return WorkspaceSyncService(
        env_status_svc=env_status_svc,
        worktree_repo=fake_worktree_repo,  # type: ignore[arg-type]
        repo_repo=repo_repo,  # type: ignore[arg-type]
        repo_factory=RepositoryFactory(config),
        workspace=workspace,
        git_ops=git_ops,
        git_repo=git_repo,  # type: ignore[arg-type]
        config_lock_repo=config_lock_repo,  # type: ignore[arg-type]
    )


# ── Service-level tests ────────────────────────────────────────────────────────


def test_update_tag_pin_moves_to_new_sha(tmp_path: Path) -> None:
    """Tag pin re-resolved to a new SHA → checkout_detached to new SHA + lock rewritten + re_pinned."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_path)
    git_repo.resolved_refs[(repo_path, "v1.4.2")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_path] = SHA_OLD  # HEAD is at the old SHA

    existing_entry = LockEntry(name="my-lib", ref="v1.4.2", kind=RefKind.tag, commit=SHA_OLD)
    lock_repo = FakeConfigLockRepository(entries={"my-lib": existing_entry})
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.4.2")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    # checkout_detached called with the new SHA.
    assert (repo_path, SHA_NEW) in git_repo.detached_checkouts
    # Lock was rewritten.
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    assert written["my-lib"].commit == SHA_NEW
    assert written["my-lib"].kind is RefKind.tag
    # Outcome is re_pinned with short SHA.
    assert report.standalone[0].sync_result == SyncResult.re_pinned
    assert report.standalone[0].pin_ref == SHA_NEW[:8]
    assert ("standalone", "my-lib", SyncResult.re_pinned, SHA_NEW[:8]) in reporter.synced
    assert reporter.completed_success is True


def test_update_branch_pin_resets_to_resolved_commit(tmp_path: Path) -> None:
    """Branch pin → checkout_branch called + lock rewritten."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_path)
    git_repo.resolved_refs[(repo_path, "main")] = (RefKind.branch, SHA_NEW)
    git_repo.head_commits[repo_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="main")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    assert (repo_path, "main") in git_repo.branch_checkouts
    assert git_repo.detached_checkouts == []
    assert len(lock_repo.write_calls) == 1
    assert lock_repo.write_calls[0]["my-lib"].commit == SHA_NEW
    assert lock_repo.write_calls[0]["my-lib"].kind is RefKind.branch
    assert report.standalone[0].sync_result == SyncResult.re_pinned


def test_update_named_repo_only_repins_that_repo(tmp_path: Path) -> None:
    """<repo> targeting → only the named repo re-pinned; others untouched."""
    repo_a_path = tmp_path / "repo-a"
    repo_b_path = tmp_path / "repo-b"
    repo_a_path.mkdir()
    repo_b_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_a_path)
    git_repo.clean_worktrees.add(repo_b_path)
    git_repo.resolved_refs[(repo_a_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(repo_b_path, "v2.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_a_path] = SHA_OLD
    git_repo.head_commits[repo_b_path] = SHA_OLD

    entry_b = LockEntry(name="repo-b", ref="v2.0", kind=RefKind.tag, commit=SHA_OLD)
    lock_repo = FakeConfigLockRepository(entries={"repo-b": entry_b})
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [
            StandaloneRepositoryConfig(name="repo-a", ref="v1.0"),
            StandaloneRepositoryConfig(name="repo-b", ref="v2.0"),
        ],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=["repo-a"], autostash=False, reporter=reporter)

    # Only repo-a was re-pinned.
    assert len(report.standalone) == 1
    assert report.standalone[0].repo_name == "repo-a"
    assert report.standalone[0].sync_result == SyncResult.re_pinned
    # repo-b NOT in lock write (preserving its entry).
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    assert "repo-a" in written
    # repo-b NOT re-fetched or checked out.
    assert repo_b_path not in [p for p, _ in git_repo.detached_checkouts]
    assert repo_b_path not in [p for p, _ in git_repo.branch_checkouts]


def test_update_multiple_literal_repos_repins_each(tmp_path: Path) -> None:
    """Multiple literal repo names (`ws update a b`) re-pin exactly those repos."""
    repo_a_path = tmp_path / "repo-a"
    repo_b_path = tmp_path / "repo-b"
    repo_c_path = tmp_path / "repo-c"
    repo_a_path.mkdir()
    repo_b_path.mkdir()
    repo_c_path.mkdir()

    git_repo = FakeGitRepository()
    for path in (repo_a_path, repo_b_path, repo_c_path):
        git_repo.clean_worktrees.add(path)
        git_repo.head_commits[path] = SHA_OLD
    git_repo.resolved_refs[(repo_a_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(repo_b_path, "v2.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(repo_c_path, "v3.0")] = (RefKind.tag, SHA_NEW)

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [
            StandaloneRepositoryConfig(name="repo-a", ref="v1.0"),
            StandaloneRepositoryConfig(name="repo-b", ref="v2.0"),
            StandaloneRepositoryConfig(name="repo-c", ref="v3.0"),
        ],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=["repo-a", "repo-b"], autostash=False, reporter=reporter)

    updated_names = {o.repo_name for o in report.standalone}
    assert updated_names == {"repo-a", "repo-b"}
    assert repo_c_path not in [p for p, _ in git_repo.detached_checkouts]


def test_update_glob_pattern_repins_matching_pinned_repos(tmp_path: Path) -> None:
    """A bare glob (`ws update 'repo-*'`) re-pins every matching pinned standalone."""
    repo_a_path = tmp_path / "repo-a"
    repo_b_path = tmp_path / "repo-b"
    other_path = tmp_path / "other-lib"
    repo_a_path.mkdir()
    repo_b_path.mkdir()
    other_path.mkdir()

    git_repo = FakeGitRepository()
    for path in (repo_a_path, repo_b_path, other_path):
        git_repo.clean_worktrees.add(path)
        git_repo.head_commits[path] = SHA_OLD
    git_repo.resolved_refs[(repo_a_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(repo_b_path, "v2.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(other_path, "v3.0")] = (RefKind.tag, SHA_NEW)

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [
            StandaloneRepositoryConfig(name="repo-a", ref="v1.0"),
            StandaloneRepositoryConfig(name="repo-b", ref="v2.0"),
            StandaloneRepositoryConfig(name="other-lib", ref="v3.0"),
        ],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=["repo-*"], autostash=False, reporter=reporter)

    updated_names = {o.repo_name for o in report.standalone}
    assert updated_names == {"repo-a", "repo-b"}
    assert other_path not in [p for p, _ in git_repo.detached_checkouts]


def test_update_glob_matching_no_pinned_repo_is_a_noop(tmp_path: Path) -> None:
    """A glob matching zero pinned standalones is a no-op — no error, empty report."""
    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()
    git_repo = FakeGitRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=["zzz-*"], autostash=False, reporter=reporter)

    assert report.standalone == []
    assert reporter.completed_success is True


def test_update_bare_repins_all_pinned_standalones(tmp_path: Path) -> None:
    """Bare update → all pinned standalones; non-pinned (ref=None) untouched."""
    repo_a_path = tmp_path / "repo-a"
    repo_b_path = tmp_path / "repo-b"
    repo_unpinned_path = tmp_path / "repo-unpinned"
    repo_a_path.mkdir()
    repo_b_path.mkdir()
    repo_unpinned_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_a_path)
    git_repo.clean_worktrees.add(repo_b_path)
    git_repo.resolved_refs[(repo_a_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.resolved_refs[(repo_b_path, "v2.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_a_path] = SHA_OLD
    git_repo.head_commits[repo_b_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [
            StandaloneRepositoryConfig(name="repo-a", ref="v1.0"),
            StandaloneRepositoryConfig(name="repo-b", ref="v2.0"),
            StandaloneRepositoryConfig(name="repo-unpinned", ref=None),
        ],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    # Only pinned repos in report.
    updated_names = {o.repo_name for o in report.standalone}
    assert "repo-a" in updated_names
    assert "repo-b" in updated_names
    assert "repo-unpinned" not in updated_names
    # Non-pinned was NOT fetched by update_pins.
    assert "repo-unpinned" not in repo_repo.fetched_standalones


def test_update_dirty_without_autostash_is_refused(tmp_path: Path) -> None:
    """dirty + no --autostash → REFUSED: no checkout, no lock write, pin_error outcome."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    # NOT added to clean_worktrees → dirty.
    git_repo.resolved_refs[(repo_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    # No checkout was called.
    assert git_repo.detached_checkouts == []
    assert git_repo.branch_checkouts == []
    # No stash operations.
    assert git_repo.stash_pushes == []
    assert git_repo.stash_pops == []
    # No lock write.
    assert lock_repo.write_calls == []
    # Outcome is pin_error (dirty guard refused the re-pin).
    assert report.standalone[0].sync_result == SyncResult.pin_error
    assert reporter.completed_success is False


def test_update_dirty_with_autostash_succeeds(tmp_path: Path) -> None:
    """dirty + --autostash → stash_push before checkout, stash_pop after."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    # NOT in clean_worktrees → dirty.
    git_repo.resolved_refs[(repo_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=True, reporter=reporter)

    # stash_push was called before checkout.
    assert repo_path in git_repo.stash_pushes
    # checkout happened.
    assert (repo_path, SHA_NEW) in git_repo.detached_checkouts
    # stash_pop was called after checkout.
    assert repo_path in git_repo.stash_pops
    # Lock was rewritten.
    assert len(lock_repo.write_calls) == 1
    assert report.standalone[0].sync_result == SyncResult.re_pinned
    assert reporter.completed_success is True


def test_update_unresolvable_ref_per_repo_failure_fanout_continues(tmp_path: Path) -> None:
    """Unresolvable ref → per-repo failure (pin_error), fan-out continues to next repo."""
    repo_a_path = tmp_path / "repo-a"
    repo_b_path = tmp_path / "repo-b"
    repo_a_path.mkdir()
    repo_b_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_a_path)
    git_repo.clean_worktrees.add(repo_b_path)
    # repo-a: no resolved_ref (will raise RepoError).
    git_repo.resolved_refs[(repo_b_path, "v2.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_a_path] = SHA_OLD
    git_repo.head_commits[repo_b_path] = SHA_OLD

    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [
            StandaloneRepositoryConfig(name="repo-a", ref="nonexistent-tag"),
            StandaloneRepositoryConfig(name="repo-b", ref="v2.0"),
        ],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    # Both repos in report.
    outcomes = {o.repo_name: o.sync_result for o in report.standalone}
    assert outcomes["repo-a"] == SyncResult.pin_error
    assert outcomes["repo-b"] == SyncResult.re_pinned
    # repo-b was checked out and lock written.
    assert (repo_b_path, SHA_NEW) in git_repo.detached_checkouts
    assert len(lock_repo.write_calls) == 1


def test_update_resolved_equals_current_is_up_to_date(tmp_path: Path) -> None:
    """Resolved commit == current HEAD and lock already records it → up_to_date, no lock churn."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_path)
    git_repo.resolved_refs[(repo_path, "v1.0")] = (RefKind.tag, SHA_OLD)
    git_repo.head_commits[repo_path] = SHA_OLD  # HEAD is already at the resolved SHA

    existing_entry = LockEntry(name="my-lib", ref="v1.0", kind=RefKind.tag, commit=SHA_OLD)
    lock_repo = FakeConfigLockRepository(entries={"my-lib": existing_entry})
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _RecordingPullReporter()
    report = svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    # No checkout.
    assert git_repo.detached_checkouts == []
    assert git_repo.branch_checkouts == []
    # No lock write.
    assert lock_repo.write_calls == []
    # Outcome is up_to_date.
    assert report.standalone[0].sync_result == SyncResult.up_to_date
    assert reporter.completed_success is True


def test_update_other_repos_lock_entries_preserved(tmp_path: Path) -> None:
    """Other repos' lock entries are preserved across a re-pin rewrite."""
    repo_path = tmp_path / "my-lib"
    repo_path.mkdir()

    git_repo = FakeGitRepository()
    git_repo.clean_worktrees.add(repo_path)
    git_repo.resolved_refs[(repo_path, "v1.0")] = (RefKind.tag, SHA_NEW)
    git_repo.head_commits[repo_path] = SHA_OLD

    other_entry = LockEntry(name="other-repo", ref="v2.0", kind=RefKind.tag, commit=SHA_OTHER)
    lock_repo = FakeConfigLockRepository(entries={"other-repo": other_entry})
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    reporter = _NullPullReporter()  # type: ignore[assignment]
    svc.update_pins(repo_patterns=[], autostash=False, reporter=reporter)

    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    assert written["my-lib"].commit == SHA_NEW
    assert written["other-repo"] == other_entry


def test_update_named_repo_not_found_raises(tmp_path: Path) -> None:
    """Named repo not a pinned standalone → raises RepoError from update_pins."""
    git_repo = FakeGitRepository()
    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref="v1.0")],
        repo_repo,
        git_repo,
        lock_repo,
    )
    with pytest.raises(RepoError, match="no pinned standalone repo named"):
        svc.update_pins(repo_patterns=["nonexistent"], autostash=False, reporter=_NullPullReporter())  # type: ignore[arg-type]


def test_update_named_repo_exists_but_unpinned_raises(tmp_path: Path) -> None:
    """Named repo exists but has no ref → raises RepoError with helpful message."""
    git_repo = FakeGitRepository()
    lock_repo = FakeConfigLockRepository()
    repo_repo = FakeWriteRepoRepository()

    svc = _make_update_service(
        tmp_path,
        [StandaloneRepositoryConfig(name="my-lib", ref=None)],
        repo_repo,
        git_repo,
        lock_repo,
    )
    with pytest.raises(RepoError, match="no `ref` configured"):
        svc.update_pins(repo_patterns=["my-lib"], autostash=False, reporter=_NullPullReporter())  # type: ignore[arg-type]


# ── CLI layer tests ────────────────────────────────────────────────────────────


def test_ws_update_help_shows_command_and_autostash() -> None:
    """ws update --help shows the command surface including --autostash."""
    from click.testing import CliRunner

    from winter_cli.modules.workspace.command import ws_update

    runner = CliRunner()
    result = runner.invoke(ws_update, ["--help"])
    assert result.exit_code == 0
    assert "--autostash" in result.output
    assert "update" in result.output.lower() or "re-resolve" in result.output.lower() or "ref" in result.output.lower()


def test_ws_update_handler_routes_bare_form(tmp_path: Path) -> None:
    """Handler.update() with repos=[] calls update_pins(repo_patterns=[], ...)."""
    from unittest.mock import MagicMock

    from winter_cli.modules.workspace.models import PullReport

    mock_sync_svc = MagicMock()
    mock_sync_svc.update_pins.return_value = PullReport(envs=[], standalone=[], skipped=[])

    reporter_factory = MagicMock()
    reporter_factory.get_pull_reporter.return_value = _NullPullReporter()

    handler = WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=mock_sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=MagicMock(),
        workspace=MagicMock(),
    )

    handler.update(EnvUpdateParams(repos=[], autostash=False, output_json=False))

    mock_sync_svc.update_pins.assert_called_once_with(
        repo_patterns=[],
        autostash=False,
        reporter=reporter_factory.get_pull_reporter.return_value,
    )


def test_ws_update_handler_routes_named_form(tmp_path: Path) -> None:
    """Handler.update() with repos=['my-lib'] passes that name through."""
    from unittest.mock import MagicMock

    from winter_cli.modules.workspace.models import PullReport

    mock_sync_svc = MagicMock()
    mock_sync_svc.update_pins.return_value = PullReport(envs=[], standalone=[], skipped=[])

    reporter_factory = MagicMock()
    reporter_factory.get_pull_reporter.return_value = _NullPullReporter()

    handler = WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=mock_sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=MagicMock(),
        workspace=MagicMock(),
    )

    handler.update(EnvUpdateParams(repos=["my-lib"], autostash=True, output_json=False))

    mock_sync_svc.update_pins.assert_called_once_with(
        repo_patterns=["my-lib"],
        autostash=True,
        reporter=reporter_factory.get_pull_reporter.return_value,
    )


def test_ws_update_handler_propagates_repo_error_as_click_exception(tmp_path: Path) -> None:
    """A RepoError from update_pins surfaces as a ClickException (clear error message)."""
    from unittest.mock import MagicMock

    import click

    mock_sync_svc = MagicMock()
    mock_sync_svc.update_pins.side_effect = RepoError("no pinned standalone repo named 'bogus'", cwd="")

    reporter_factory = MagicMock()
    reporter_factory.get_pull_reporter.return_value = _NullPullReporter()

    handler = WorkspaceHandler(
        env_status_svc=MagicMock(),
        workspace_sync_svc=mock_sync_svc,
        workspace_push_svc=MagicMock(),
        workspace_merge_svc=MagicMock(),
        env_checkout_svc=MagicMock(),
        workspace_repo=MagicMock(),
        repo_repo=MagicMock(),
        repo_factory=MagicMock(),
        drift_warning_svc=MagicMock(),
        prune_svc=MagicMock(),
        reporter_factory=reporter_factory,
        cli_output_svc=MagicMock(),
        workspace=MagicMock(),
    )

    with pytest.raises(click.ClickException, match="no pinned standalone repo named"):
        handler.update(EnvUpdateParams(repos=["bogus"], autostash=False, output_json=False))
