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
    GitIdentity,
    ProjectRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
    )


def _service(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    subprocess: FakeSubprocessRunner,
    git: FakeGitRepository,
) -> InitService:
    ext_svc = ExtensionService(
        workspace_config,
        fs=fs,
        config_file_reader=FakeConfigFileReader({}),
        subprocess_runner=subprocess,
    )
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config),
        extension_svc=ext_svc,
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
    )


def test_reconcile_projects_clones_missing_repo(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """The first reconcile clones the source checkout, applies identity, runs no cmds (none declared)."""
    fs = FakeFilesystem()  # nothing on disk yet
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    # Simulate the workspace's .git/info/ being present so the self-exclude
    # path is reachable; init writes through `_fs.write_text` so we can
    # observe the resulting content in `fs.files`.
    fs.directories.add(WORKSPACE_ROOT / ".git")
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    # Clone was invoked through IGitRepository.
    assert git.clones == [("git@example.com:org/demo.git", WORKSPACE_ROOT / "projects" / "demo")]
    # Identity applied.
    assert git.identities == [(WORKSPACE_ROOT / "projects" / "demo", "Bot", "bot@example.com")]
    # Reporter saw the high-level events.
    assert ("demo", str(WORKSPACE_ROOT / "projects" / "demo"), "cloned", "") in init_reporter.actions
    assert ("projects/", True) in init_reporter.targets_completed


def test_reconcile_projects_skips_clone_when_checkout_present(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    assert git.clones == []  # already on disk; no clone
    # "exists" action recorded instead of "cloned"
    assert any(a[2] == "exists" for a in init_reporter.actions)


def test_reconcile_env_creates_worktree_and_seeds_env_file(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Happy path for `winter ws init alpha`: worktree created, .winter.env written, identity applied."""
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()
    git.local_branches[demo_path] = ["main"]  # branch "alpha" doesn't exist yet

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is True
    # Worktree created with -b alpha main.
    assert git.added_worktrees == [(demo_path, WORKSPACE_ROOT / "alpha" / "demo", "alpha", "main")]
    # .winter.env seeded with the env's port window.
    env_file = WORKSPACE_ROOT / "alpha" / ".winter.env"
    assert env_file in fs.files
    content = fs.files[env_file]
    assert "WINTER_ENV=alpha" in content
    assert "WINTER_ENV_INDEX=1" in content
    assert "WINTER_PORT_BASE=4100" in content
    # Identity applied to the worktree.
    assert (WORKSPACE_ROOT / "alpha" / "demo", "Bot", "bot@example.com") in git.identities


def test_reconcile_env_fails_when_source_checkout_missing(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """`reconcile_env` reports an error and continues when the project repo isn't cloned yet."""
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects"])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is False
    error_messages = [error for _, error in init_reporter.errors]
    assert any("source checkout missing" in msg for msg in error_messages)
    # No worktree-add attempted on a missing source.
    assert git.added_worktrees == []


def test_run_cmds_streams_output_through_reporter(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """A project repo with a `cmd` list runs each command via the subprocess seam."""
    cfg = workspace_config.model_copy(
        update={
            "project_repos": [
                ProjectRepositoryConfig(
                    name="demo", url="git@example.com:org/demo.git", cmd=["pnpm install"]
                )
            ]
        }
    )
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    subprocess = FakeSubprocessRunner(
        popen_responses={"pnpm install": (["+ pnpm install line 1", "Done"], 0)},
    )
    # Also stub run() for any subprocess.run calls (none expected here, but keep
    # the runner permissive by registering no entries — assertion fires only
    # on misroute, which we'd want to catch).
    _ = SubprocessResult  # imported for type completeness
    git = FakeGitRepository()

    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    assert ("demo", "+ pnpm install line 1") in init_reporter.cmd_output
    assert ("demo", "Done") in init_reporter.cmd_output
    assert ("demo", "pnpm install", 0) in init_reporter.cmds_completed
