from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeFilesystem,
    FakeGitRepository,
    FakeInitReporter,
    FakeSubprocessRunner,
)
from winter_cli.config.models import (
    AdoptExtensions,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")
DEMO_MAIN = WORKSPACE_ROOT / "projects" / "demo"


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
    )


def _service(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    git: FakeGitRepository,
) -> DestroyService:
    ext_svc = ExtensionService(
        workspace_config,
        fs=fs,
        config_file_reader=FakeConfigFileReader({}),
        subprocess_runner=FakeSubprocessRunner(),
    )
    return DestroyService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config),
        extension_svc=ext_svc,
        fs=fs,
        git_repo=git,
    )


def test_destroy_env_removes_worktree_dir_and_env_dir(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
        files={
            env_root / ".winter.env": "WINTER_ENV=alpha\n",
            WORKSPACE_ROOT / ".git" / "info" / "exclude": "",
        },
    )
    git = FakeGitRepository()
    git.clean_worktrees.add(worktree_path)  # so the dirty-check passes

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=init_reporter)

    assert ok is True
    # IGitRepository.remove_worktree called against the source checkout.
    assert git.removed_worktrees == [(DEMO_MAIN, worktree_path, False)]
    # rmtree-equivalent on the env root: the FakeFilesystem drops it.
    assert not fs.exists(env_root)
    # Reporter saw the env_removed action.
    assert any(a[2] == "env_removed" for a in init_reporter.actions)


def test_destroy_env_refuses_dirty_worktree_without_force(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
    )
    git = FakeGitRepository()  # worktree_path NOT marked clean → dirty

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=init_reporter)

    assert ok is False
    # Nothing was removed.
    assert git.removed_worktrees == []
    assert fs.exists(env_root)
    error_messages = [error for _, error in init_reporter.errors]
    assert any("dirty worktrees" in msg for msg in error_messages)


def test_destroy_env_dry_run_emits_actions_but_does_not_remove(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    exclude_path = WORKSPACE_ROOT / ".git" / "info" / "exclude"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
        files={
            exclude_path: "# >>> winter-dir/alpha (managed by winter)\n/alpha/\n# <<< winter-dir/alpha (managed by winter)\n"
        },
    )
    git = FakeGitRepository()
    git.clean_worktrees.add(worktree_path)  # dirty-check passes; dry-run only checks intent

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=True, reporter=init_reporter)

    assert ok is True
    assert git.removed_worktrees == []  # nothing actually removed
    assert fs.exists(env_root)
    action_kinds = [a[2] for a in init_reporter.actions]
    assert "would_remove_worktree" in action_kinds
    assert "would_remove_env" in action_kinds
    assert "would_remove_workspace_exclude" in action_kinds


def test_destroy_env_strips_workspace_exclude_block(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """The `winter-dir/<env>` managed block is removed from .git/info/exclude."""
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    exclude_path = WORKSPACE_ROOT / ".git" / "info" / "exclude"
    initial = (
        "# unrelated user line\n"
        "# >>> winter-dir/alpha (managed by winter)\n"
        "/alpha/\n"
        "# <<< winter-dir/alpha (managed by winter)\n"
    )
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
        files={exclude_path: initial},
    )
    git = FakeGitRepository()
    git.clean_worktrees.add(worktree_path)

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=init_reporter)

    assert ok is True
    remaining = fs.files[exclude_path]
    assert "winter-dir/alpha" not in remaining
    assert "# unrelated user line" in remaining


def test_destroy_env_with_missing_source_checkout_falls_back_to_rmtree(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """When the source checkout is gone too, the worktree dir is removed via rmtree."""
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    fs = FakeFilesystem(directories=[env_root, worktree_path])  # no source checkout
    git = FakeGitRepository()
    git.clean_worktrees.add(worktree_path)

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=True, strict=False, dry_run=False, reporter=init_reporter)

    assert ok is True
    assert git.removed_worktrees == []  # IGitRepository.remove_worktree not called
    assert not fs.exists(env_root)
    actions = [(a[0], a[2], a[3]) for a in init_reporter.actions]
    assert ("demo", "worktree_removed", "no source checkout") in actions


class _ExplodingRemoveGit(FakeGitRepository):
    """FakeGitRepository whose remove_worktree raises — exercises the per-repo wrap site."""

    def remove_worktree(self, source: Path, worktree_path: Path, force: bool) -> None:  # type: ignore[override]
        raise RepoError("worktree busy")


def test_remove_git_worktree_failure_caught_at_wrap_site(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """A leaf RepoError from `remove_worktree` is caught once per repo;
    the aggregator records failure but continues to subsequent phases."""
    env_root = WORKSPACE_ROOT / "alpha"
    worktree_path = env_root / "demo"
    fs = FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
    )
    git = _ExplodingRemoveGit()
    git.clean_worktrees.add(worktree_path)  # dirty-check passes

    svc = _service(workspace_config, fs, git)
    ok = svc.destroy_env("alpha", force=False, strict=False, dry_run=False, reporter=init_reporter)

    assert ok is False
    demo_errors = [msg for repo, msg in init_reporter.errors if repo == "demo"]
    assert len(demo_errors) == 1
    assert "worktree busy" in demo_errors[0]
    # Phase 4 still ran — env directory was removed despite the worktree failure.
    assert not fs.exists(env_root)
