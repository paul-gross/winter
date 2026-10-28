from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeConfigLockRepository,
    FakeEnvIndexRegistry,
    FakeFilesystem,
    FakeGitRepository,
    FakeInitReporter,
    FakeSubprocessRunner,
    FakeWorkspaceExcludeLocator,
    make_workspace_config,
)
from tests.modules.workspace.conftest import FakeLocalOverlayRepository, FakeNestedWorkspaceRunner, make_nested_service
from winter_cli.config.models import (
    AdoptExtensions,
    ExtensionLoad,
    GitIdentity,
    ProjectRepositoryConfig,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.workspace.extension_agentsmd_service import ExtensionAgentsMdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
from winter_cli.modules.workspace.nested_workspace_service import NestedWorkspaceService
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
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
    git_ops: GitOpsService | None = None,
    config_lock_repo: FakeConfigLockRepository | None = None,
    registry: FakeEnvIndexRegistry | None = None,
    nested_svc: NestedWorkspaceService | None = None,
) -> InitService:
    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({}))
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config, fs=fs),
        extension_symlink_svc=ExtensionSymlinkService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        extension_hook_svc=ExtensionHookService(
            config=workspace_config,
            fs=fs,
            subprocess_runner=subprocess,
            manifest_loader=manifest_loader,
        ),
        extension_exclude_svc=ExtensionExcludeService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
            exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
        ),
        extension_agentsmd_svc=ExtensionAgentsMdService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=git_ops or GitOpsService(RepoErrorFactory()),
        registry=registry or FakeEnvIndexRegistry(),
        config_lock_repo=config_lock_repo,
        exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
        nested_svc=nested_svc,
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
    assert git.repo_names_for("clone") == ["demo"]
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


def test_reconcile_env_creates_worktree_with_identity(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Happy path for `winter ws init alpha`: worktree created and identity applied.

    Env variables are no longer written to a .winter.env file — they are computed
    on-demand by EnvProvisionerService and injected at runtime.
    """
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
    # No .winter.env file written — env is injected at runtime.
    assert (WORKSPACE_ROOT / "alpha" / ".winter.env") not in fs.files
    # Identity applied to the worktree.
    assert (WORKSPACE_ROOT / "alpha" / "demo", "Bot", "bot@example.com") in git.identities


def test_reconcile_projects_writes_logs_exclude(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """reconcile_projects writes a managed git-exclude block for /.winter/logs/.

    No .winter.workspace.env file is written — env is computed on-demand.
    """
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git")
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    # No .winter.workspace.env file — env is injected at runtime.
    assert (WORKSPACE_ROOT / ".winter.workspace.env") not in fs.files
    # Only /.winter/logs/ is excluded (not .winter.workspace.env).
    exclude = fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"]
    assert "# >>> winter-workspace/artifacts (managed by winter)" in exclude
    assert "/.winter/logs/" in exclude
    assert "/.winter.workspace.env" not in exclude


def test_reconcile_projects_exclude_block_idempotent(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Re-running reconcile_projects does not duplicate the managed git-exclude block."""
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git")
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    assert svc.reconcile_projects(init_reporter) is True
    assert svc.reconcile_projects(init_reporter) is True

    exclude = fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"]
    # Block appears exactly once even after two runs.
    assert exclude.count("# >>> winter-workspace/artifacts (managed by winter)") == 1
    assert exclude.count("/.winter/logs/") == 1


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
                ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git", cmd=["pnpm install"])
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


def test_run_per_repo_caps_parallelism_via_git_ops_executor(
    init_reporter: FakeInitReporter,
) -> None:
    """Init fans out through GitOpsService.executor() so it inherits the SSH-safe cap.

    With many more repos than `PARALLELISM`, the underlying thread pool must still
    be sized to the cap — otherwise large workspaces overwhelm Codeberg's SSH
    connection limit on `winter ws init`.
    """
    repo_count = GitOpsService.PARALLELISM * 2 + 1
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name=f"r{i}", url=f"git@example.com:org/r{i}.git") for i in range(repo_count)
        ],
    )
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git")
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    git_ops = GitOpsService(RepoErrorFactory())
    observed_max_workers: list[int] = []
    real_executor = git_ops.executor

    @contextlib.contextmanager
    def spying_executor():
        with real_executor() as pool:
            observed_max_workers.append(pool._max_workers)
            yield pool

    git_ops.executor = spying_executor  # type: ignore[method-assign]

    svc = _service(cfg, fs, subprocess, git, git_ops=git_ops)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    assert observed_max_workers, "InitService should fan out via git_ops.executor()"
    assert all(workers == GitOpsService.PARALLELISM for workers in observed_max_workers), (
        f"expected each fan-out capped at {GitOpsService.PARALLELISM}, got {observed_max_workers}"
    )


class _ExplodingGitRepository(FakeGitRepository):
    """FakeGitRepository whose clone raises RepoError — exercises the per-repo wrap site."""

    def clone(self, url: str, dest: Path, *, repo_name: str) -> None:
        raise RepoError(f"boom cloning {url}")


def test_per_repo_wrap_catches_leaf_repo_error(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """A RepoError raised by a leaf (clone) is caught at the per-repo wrap site:
    reporter sees one repo_error, the entrypoint returns False, no exception escapes."""
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = _ExplodingGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is False
    error_messages = [error for _, error in init_reporter.errors]
    assert any("boom cloning" in msg for msg in error_messages)
    # The wrap site reports exactly once per failing repo — no duplicate
    # catch-log-rethrow chains.
    demo_errors = [msg for repo, msg in init_reporter.errors if repo == "demo"]
    assert len(demo_errors) == 1
    # target_completed still fires with success=False.
    assert ("projects/", False) in init_reporter.targets_completed


def test_run_cmds_failure_surfaces_via_wrap_site(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """A non-zero command exit raises from `_run_cmds` and is caught at the
    per-repo wrap site — the entrypoint returns False with a single error."""
    cfg = workspace_config.model_copy(
        update={
            "project_repos": [ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git", cmd=["false"])]
        }
    )
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner(popen_responses={"false": ([], 1)})
    git = FakeGitRepository()

    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_projects(init_reporter)

    assert ok is False
    demo_errors = [msg for repo, msg in init_reporter.errors if repo == "demo"]
    assert len(demo_errors) == 1
    assert "exited with code 1" in demo_errors[0]


# ── on_workspace_reconcile wiring tests ───────────────────────────────────────


def _make_extension_config(fs: FakeFilesystem, config_files: dict, name: str) -> StandaloneRepositoryConfig:
    """Register a fake standalone extension with an on_workspace_reconcile hook in fs."""
    ext_path = WORKSPACE_ROOT / name
    fs.directories.add(ext_path)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files[manifest_path] = {
        "name": name,
        "hooks": {"on_workspace_reconcile": "hooks/ws-reconcile.sh"},
    }
    hook_path = (ext_path / "hooks" / "ws-reconcile.sh").resolve()
    fs.files[hook_path] = ""
    fs.executables.add(hook_path)
    fs.directories.add(hook_path.parent)
    return StandaloneRepositoryConfig(name=name, url=f"git@example.com:org/{name}.git")


def _service_with_ext(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict,
    subprocess: FakeSubprocessRunner,
    git: FakeGitRepository,
) -> InitService:
    """Build an InitService whose manifest loader uses the canned config_files dict."""
    from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
    from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader

    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files))
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config, fs=fs),
        extension_symlink_svc=ExtensionSymlinkService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        extension_hook_svc=ExtensionHookService(
            config=workspace_config,
            fs=fs,
            subprocess_runner=subprocess,
            manifest_loader=manifest_loader,
        ),
        extension_exclude_svc=ExtensionExcludeService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
            exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
        ),
        extension_agentsmd_svc=ExtensionAgentsMdService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=GitOpsService(RepoErrorFactory()),
        registry=FakeEnvIndexRegistry(),
        exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
    )


def test_workspace_reconcile_hook_fires_once_on_reconcile_all(
    init_reporter: FakeInitReporter,
) -> None:
    """reconcile_all fires on_workspace_reconcile exactly once, after standalones."""
    config_files: dict = {}
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    fs.directories.add(WORKSPACE_ROOT / "projects")

    ext_cfg = _make_extension_config(fs, config_files, "my-ext")
    ext_path = WORKSPACE_ROOT / "my-ext"
    hook_path = (ext_path / "hooks" / "ws-reconcile.sh").resolve()

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
        standalone_repos=[ext_cfg],
    )

    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs.directories.add(demo_path)

    subprocess = FakeSubprocessRunner(
        popen_responses={str(hook_path): (["workspace reconcile ran"], 0)},
    )
    git = FakeGitRepository()
    git.local_branches[demo_path] = ["main"]

    # reconcile_all discovers no existing worktrees (empty list_worktrees), so it
    # only calls reconcile_projects + reconcile_standalones + workspace hook + zero envs.
    git.worktree_paths[demo_path] = [demo_path]  # only the source checkout itself

    svc = _service_with_ext(cfg, fs, config_files, subprocess, git)
    ok = svc.reconcile_all(init_reporter)

    assert ok is True
    ws_reconcile_calls = [call for call, _ in subprocess.popen_calls if str(hook_path) in str(call)]
    assert len(ws_reconcile_calls) == 1, (
        f"expected exactly 1 on_workspace_reconcile call, got {len(ws_reconcile_calls)}"
    )


def _make_project_extension_config(fs: FakeFilesystem, config_files: dict, name: str) -> ProjectRepositoryConfig:
    """Register a project repo whose root carries a winter-ext.toml with an
    on_workspace_reconcile hook — mirrors _make_extension_config for standalones."""
    ext_path = WORKSPACE_ROOT / "projects" / name
    fs.directories.add(ext_path)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files[manifest_path] = {
        "name": name,
        "hooks": {"on_workspace_reconcile": "hooks/ws-reconcile.sh"},
    }
    hook_path = (ext_path / "hooks" / "ws-reconcile.sh").resolve()
    fs.files[hook_path] = ""
    fs.executables.add(hook_path)
    fs.directories.add(hook_path.parent)
    return ProjectRepositoryConfig(name=name, url=f"git@example.com:org/{name}.git")


def test_workspace_reconcile_hook_fires_for_project_repo_extension(
    init_reporter: FakeInitReporter,
) -> None:
    """A project repo carrying a root winter-ext.toml (no standalone counterpart)
    fires its on_workspace_reconcile hook too — get_extension_repos() folds it in."""
    config_files: dict = {}
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    fs.directories.add(WORKSPACE_ROOT / "projects")

    proj_cfg = _make_project_extension_config(fs, config_files, "winter-docs")
    ext_path = WORKSPACE_ROOT / "projects" / "winter-docs"
    hook_path = (ext_path / "hooks" / "ws-reconcile.sh").resolve()

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[proj_cfg],
    )

    git = FakeGitRepository()
    git.local_branches[ext_path] = ["main"]
    git.worktree_paths[ext_path] = [ext_path]  # only the source checkout itself

    subprocess = FakeSubprocessRunner(popen_responses={str(hook_path): (["workspace reconcile ran"], 0)})

    svc = _service_with_ext(cfg, fs, config_files, subprocess, git)
    ok = svc.reconcile_all(init_reporter)

    assert ok is True
    ws_reconcile_calls = [call for call, _ in subprocess.popen_calls if str(hook_path) in str(call)]
    assert len(ws_reconcile_calls) == 1, (
        f"expected exactly 1 on_workspace_reconcile call for the project-repo extension, got {len(ws_reconcile_calls)}"
    )


def test_workspace_reconcile_hook_fires_once_on_no_target_path(
    init_reporter: FakeInitReporter,
) -> None:
    """The no-target path (reconcile_projects + reconcile_standalones) fires the hook once."""
    config_files: dict = {}
    fs = FakeFilesystem()
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    fs.directories.add(WORKSPACE_ROOT / "projects")

    ext_cfg = _make_extension_config(fs, config_files, "my-ext")
    ext_path = WORKSPACE_ROOT / "my-ext"
    hook_path = (ext_path / "hooks" / "ws-reconcile.sh").resolve()

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
        standalone_repos=[ext_cfg],
    )

    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs.directories.add(demo_path)

    subprocess = FakeSubprocessRunner(
        popen_responses={str(hook_path): (["workspace hook ran"], 0)},
    )
    git = FakeGitRepository()

    svc = _service_with_ext(cfg, fs, config_files, subprocess, git)

    # Simulate the no-target path: reconcile_projects then reconcile_standalones,
    # then run_workspace_reconcile_hooks (as the handler does).
    ok = svc.reconcile_projects(init_reporter)
    if not svc.reconcile_standalones(init_reporter):
        ok = False
    if not svc.run_workspace_reconcile_hooks(init_reporter):
        ok = False

    assert ok is True
    ws_reconcile_calls = [call for call, _ in subprocess.popen_calls if str(hook_path) in str(call)]
    assert len(ws_reconcile_calls) == 1, (
        f"expected exactly 1 on_workspace_reconcile call, got {len(ws_reconcile_calls)}"
    )


def test_workspace_reconcile_hook_does_not_fire_on_reconcile_env(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """reconcile_env must NOT trigger on_workspace_reconcile."""
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()
    git.local_branches[demo_path] = ["main"]

    svc = _service(workspace_config, fs, subprocess, git)
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is True
    # No subprocess calls at all — the workspace reconcile hook must not fire.
    assert not subprocess.popen_calls, "on_workspace_reconcile must NOT fire inside reconcile_env"


# ── Phase 4: standalone pin tests ────────────────────────────────────────────

STANDALONE_SHA = "a" * 40  # full 40-char fake SHA


def _standalone_config(ref: str | None = None) -> WorkspaceConfig:
    """WorkspaceConfig with one standalone repo, optionally pinned to `ref`."""
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        standalone_repos=[
            StandaloneRepositoryConfig(
                name="my-ext",
                url="git@example.com:org/my-ext.git",
                ref=ref,
            )
        ],
    )


def _standalone_fs() -> FakeFilesystem:
    """Filesystem with the standalone repo already cloned (exists on disk)."""
    ext_path = WORKSPACE_ROOT / "my-ext"
    fs = FakeFilesystem(directories=[ext_path])
    return fs


def test_pin_lock_fresh_tag_uses_locked_commit_no_resolve(
    init_reporter: FakeInitReporter,
) -> None:
    """lock present + fresh + tag → checkout_detached at locked commit; resolve_ref NOT called; lock NOT rewritten."""
    cfg = _standalone_config(ref="v1.0.0")
    ext_path = WORKSPACE_ROOT / "my-ext"

    locked_sha = STANDALONE_SHA
    lock_repo = FakeConfigLockRepository(
        entries={"my-ext": LockEntry(name="my-ext", ref="v1.0.0", kind=RefKind.tag, commit=locked_sha)}
    )
    git = FakeGitRepository()
    # Mark the worktree clean (would be needed for stale path, not used here but safe to set).
    git.clean_worktrees.add(ext_path)

    svc = _service(cfg, FakeFilesystem(directories=[ext_path]), FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # checkout_detached called with the locked commit.
    assert git.detached_checkouts == [(ext_path, locked_sha)]
    # checkout_branch NOT called.
    assert git.branch_checkouts == []
    # resolve_ref NOT called — no entry in resolved_refs means calling it would raise.
    # (FakeGitRepository.resolve_ref raises RepoError on miss; if it had been called
    #  the test would have failed with a RepoError surfaced as ok=False.)
    # Lock NOT rewritten.
    assert lock_repo.write_calls == []
    # Reporter received a "pinned" action.
    pinned_actions = [a for a in init_reporter.actions if a[2] == "pinned"]
    assert len(pinned_actions) == 1
    assert "tag" in pinned_actions[0][3]
    assert locked_sha[:8] in pinned_actions[0][3]


def test_pin_lock_fresh_branch_uses_checkout_branch_no_resolve(
    init_reporter: FakeInitReporter,
) -> None:
    """lock present + fresh + branch → checkout_branch called; resolve_ref NOT called; lock NOT rewritten."""
    cfg = _standalone_config(ref="stable")
    ext_path = WORKSPACE_ROOT / "my-ext"
    fs = _standalone_fs()

    locked_sha = "b" * 40
    lock_repo = FakeConfigLockRepository(
        entries={"my-ext": LockEntry(name="my-ext", ref="stable", kind=RefKind.branch, commit=locked_sha)}
    )
    git = FakeGitRepository()
    git.clean_worktrees.add(ext_path)

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # checkout_branch called with the ref (branch name).
    assert git.branch_checkouts == [(ext_path, "stable")]
    assert git.detached_checkouts == []
    assert lock_repo.write_calls == []
    pinned_actions = [a for a in init_reporter.actions if a[2] == "pinned"]
    assert len(pinned_actions) == 1
    assert "branch" in pinned_actions[0][3]


def test_pin_no_lock_branch_resolves_and_writes_lock(
    init_reporter: FakeInitReporter,
) -> None:
    """no lock + branch ref → resolve_ref → checkout_branch + lock written with resolved entry."""
    cfg = _standalone_config(ref="develop")
    ext_path = WORKSPACE_ROOT / "my-ext"
    fs = _standalone_fs()

    resolved_sha = "c" * 40
    lock_repo = FakeConfigLockRepository()  # empty lock
    git = FakeGitRepository()
    git.clean_worktrees.add(ext_path)
    git.resolved_refs[(ext_path, "develop")] = (RefKind.branch, resolved_sha)

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # checkout_branch called with the ref.
    assert git.branch_checkouts == [(ext_path, "develop")]
    assert git.detached_checkouts == []
    # Lock written exactly once, with the resolved entry.
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    assert "my-ext" in written
    entry = written["my-ext"]
    assert entry.ref == "develop"
    assert entry.kind is RefKind.branch
    assert entry.commit == resolved_sha
    # Reporter saw pinned action.
    pinned_actions = [a for a in init_reporter.actions if a[2] == "pinned"]
    assert len(pinned_actions) == 1


def test_pin_no_lock_tag_resolves_and_writes_lock(
    init_reporter: FakeInitReporter,
) -> None:
    """no lock + tag ref → resolve_ref → checkout_detached + lock written with kind=tag."""
    cfg = _standalone_config(ref="v2.3.0")
    ext_path = WORKSPACE_ROOT / "my-ext"
    fs = _standalone_fs()

    resolved_sha = "d" * 40
    lock_repo = FakeConfigLockRepository()
    git = FakeGitRepository()
    git.clean_worktrees.add(ext_path)
    git.resolved_refs[(ext_path, "v2.3.0")] = (RefKind.tag, resolved_sha)

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert git.detached_checkouts == [(ext_path, resolved_sha)]
    assert git.branch_checkouts == []
    assert len(lock_repo.write_calls) == 1
    entry = lock_repo.write_calls[0]["my-ext"]
    assert entry.kind is RefKind.tag
    assert entry.commit == resolved_sha
    assert entry.ref == "v2.3.0"


def test_pin_stale_lock_re_resolves_preserves_other_entries(
    init_reporter: FakeInitReporter,
) -> None:
    """stale lock (config ref changed) → re-resolves + rewrites; other repos' lock entries preserved."""
    cfg = _standalone_config(ref="v2.0.0")
    ext_path = WORKSPACE_ROOT / "my-ext"
    fs = _standalone_fs()

    old_sha = "e" * 40
    new_sha = "f" * 40
    other_entry = LockEntry(name="other-ext", ref="v1.0.0", kind=RefKind.tag, commit="0" * 40)

    lock_repo = FakeConfigLockRepository(
        entries={
            # Stale: config ref is "v2.0.0" but lock ref is "v1.0.0".
            "my-ext": LockEntry(name="my-ext", ref="v1.0.0", kind=RefKind.tag, commit=old_sha),
            "other-ext": other_entry,
        }
    )
    git = FakeGitRepository()
    git.clean_worktrees.add(ext_path)
    git.resolved_refs[(ext_path, "v2.0.0")] = (RefKind.tag, new_sha)

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # Resolved and checked out at the new commit.
    assert git.detached_checkouts == [(ext_path, new_sha)]
    # Lock rewritten once.
    assert len(lock_repo.write_calls) == 1
    written = lock_repo.write_calls[0]
    # my-ext entry updated.
    assert written["my-ext"].ref == "v2.0.0"
    assert written["my-ext"].commit == new_sha
    # other-ext entry preserved.
    assert "other-ext" in written
    assert written["other-ext"].commit == other_entry.commit


def test_pin_ref_none_skips_all_pin_machinery(
    init_reporter: FakeInitReporter,
) -> None:
    """ref is None → checkout_detached, checkout_branch, resolve_ref, and lock read/write all skipped."""
    cfg = _standalone_config(ref=None)
    fs = _standalone_fs()

    lock_repo = FakeConfigLockRepository()
    git = FakeGitRepository()

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # No pin machinery ran.
    assert git.detached_checkouts == []
    assert git.branch_checkouts == []
    assert lock_repo.write_calls == []
    # No "pinned" action reported.
    pinned_actions = [a for a in init_reporter.actions if a[2] == "pinned"]
    assert pinned_actions == []


def test_pin_dirty_stale_lock_refuses_re_resolve(
    init_reporter: FakeInitReporter,
) -> None:
    """Stale lock + dirty working tree → error reported, no checkout, no lock rewrite."""
    cfg = _standalone_config(ref="v2.0.0")
    fs = _standalone_fs()

    lock_repo = FakeConfigLockRepository(
        entries={
            "my-ext": LockEntry(name="my-ext", ref="v1.0.0", kind=RefKind.tag, commit="e" * 40),
        }
    )
    git = FakeGitRepository()
    # NOT in clean_worktrees → is_worktree_clean returns False.

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is False
    errors = [msg for _, msg in init_reporter.errors]
    assert any("uncommitted changes" in msg for msg in errors)
    assert git.detached_checkouts == []
    assert git.branch_checkouts == []
    assert lock_repo.write_calls == []


def test_reconcile_standalones_does_not_clone_project_repo_extensions(
    init_reporter: FakeInitReporter,
) -> None:
    """Regression: the git-clone loop in reconcile_standalones stays standalone-only.

    A project repo carrying a root winter-ext.toml is extension-eligible (it feeds
    AGENTS.winter.md via get_extension_repos()), but its projects/<name>/ checkout
    is reconciled by reconcile_projects, not by the standalone clone loop here — so
    reconcile_standalones must never attempt to clone or pin it.
    """
    ext_path = WORKSPACE_ROOT / "projects" / "winter-docs"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", ext_path])
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    fs.files[ext_path / "index.md"] = "# winter-docs\n"
    config_files: dict = {manifest_path: {"name": "winter-docs"}}

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        project_repos=[
            ProjectRepositoryConfig(name="winter-docs", url="git@example.com:org/winter-docs.git"),
        ],
    )
    git = FakeGitRepository()

    svc = _service_with_ext(cfg, fs, config_files, FakeSubprocessRunner(), git)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # No clone/identity/pin machinery touched the project-repo extension.
    assert git.clones == []
    # AGENTS.winter.md still renders the project-repo extension as a routing row —
    # get_extension_repos() folds it in even though the clone loop skipped it.
    agents_path = WORKSPACE_ROOT / "AGENTS.winter.md"
    assert agents_path in fs.files
    assert "winter-docs" in fs.files[agents_path]
    assert "@" not in fs.files[agents_path]


def _service_with_ext_and_agents(
    workspace_config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict,
    subprocess: FakeSubprocessRunner,
    git: FakeGitRepository,
) -> InitService:
    """Like `_service_with_ext`, but also wires ExtensionAgentService — mirroring
    the container's full wiring — so agent projection can be exercised too."""
    from winter_cli.modules.workspace.agent_install import ExtensionAgentService
    from winter_cli.modules.workspace.agent_transform.agent_enumerator import CanonicalAgentEnumerator
    from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
    from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader

    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files))
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config, fs=fs),
        extension_symlink_svc=ExtensionSymlinkService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        extension_agent_svc=ExtensionAgentService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
            agent_enumerator=CanonicalAgentEnumerator(fs=fs, manifest_loader=manifest_loader),
        ),
        extension_hook_svc=ExtensionHookService(
            config=workspace_config,
            fs=fs,
            subprocess_runner=subprocess,
            manifest_loader=manifest_loader,
        ),
        extension_exclude_svc=ExtensionExcludeService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
            exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
        ),
        extension_agentsmd_svc=ExtensionAgentsMdService(
            config=workspace_config,
            fs=fs,
            manifest_loader=manifest_loader,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=GitOpsService(RepoErrorFactory()),
        registry=FakeEnvIndexRegistry(),
        exclude_locator=FakeWorkspaceExcludeLocator(fs, workspace_config.workspace_root),
    )


def test_reconcile_standalones_projects_skills_and_agents_for_project_repo_extension(
    init_reporter: FakeInitReporter,
) -> None:
    """A project repo carrying a root winter-ext.toml (no standalone counterpart,
    e.g. winter-product) gets its skills AND agents projected identically to a
    standalone's, once its projects/<name>/ source checkout exists on disk.

    This is the regression for the observable symptom the gap left behind:
    `winter doctor` flagging "missing: <prefix>-<skill> (source exists, not
    projected)" / "missing copy: <prefix>-<agent>.md" for a project-repo-only
    extension, because `reconcile_standalones` never ran the projection step
    for anything outside `get_standalone_repos()`.
    """
    ext_path = WORKSPACE_ROOT / "projects" / "winter-product"
    skill_dir = ext_path / "skills" / "do-thing"
    agents_dir = ext_path / "agents"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", ext_path, skill_dir, agents_dir])
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    fs.files[agents_dir / "reviewer.md"] = (
        "---\nname: reviewer\ndescription: Reviews code\nmodel: sonnet\n---\nYou review code.\n"
    )
    config_files: dict = {manifest_path: {"name": "winter-product", "prefix": "wp"}}

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        project_repos=[
            ProjectRepositoryConfig(name="winter-product", url="git@example.com:org/winter-product.git"),
        ],
    )
    git = FakeGitRepository()

    svc = _service_with_ext_and_agents(cfg, fs, config_files, FakeSubprocessRunner(), git)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # No clone/identity/pin machinery touched the project-repo extension — the
    # git-clone lifecycle stays standalone-only.
    assert git.clones == []

    skill_link = WORKSPACE_ROOT / ".claude" / "skills" / "wp-do-thing"
    assert fs.is_symlink(skill_link)

    agent_copy = WORKSPACE_ROOT / ".claude" / "agents" / "wp-reviewer.md"
    assert agent_copy in fs.files


def test_reconcile_standalones_does_not_double_project_skills_for_dual_declared_repo(
    init_reporter: FakeInitReporter,
) -> None:
    """A repo declared as both [[project_repository]] (with a root winter-ext.toml)
    and [[standalone_repository]] is only projected once, via the standalone loop
    — `get_extension_repos()` dedupes the project-repo entry away, so
    `reconcile_standalones` must not also run projection at the project-repo
    checkout path for it.
    """
    standalone_path = WORKSPACE_ROOT / "my-ext"
    project_path = WORKSPACE_ROOT / "projects" / "my-ext"
    skill_dir = standalone_path / "skills" / "do-thing"
    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            standalone_path,
            project_path,
            skill_dir,
        ]
    )
    standalone_manifest = standalone_path / "winter-ext.toml"
    project_manifest = project_path / "winter-ext.toml"
    fs.files[standalone_manifest] = ""
    fs.files[project_manifest] = ""
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    config_files: dict = {
        standalone_manifest: {"name": "my-ext", "prefix": "wm"},
        project_manifest: {"name": "my-ext", "prefix": "wm"},
    }

    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        project_repos=[
            ProjectRepositoryConfig(name="my-ext", url="git@example.com:org/my-ext.git"),
        ],
        standalone_repos=[
            StandaloneRepositoryConfig(name="my-ext", url="git@example.com:org/my-ext.git"),
        ],
    )
    git = FakeGitRepository()
    git.local_branches[standalone_path] = ["main"]

    svc = _service_with_ext(cfg, fs, config_files, FakeSubprocessRunner(), git)
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    # Projected once, from the standalone checkout — never re-processed at the
    # (unused) project-repo checkout path.
    skill_link = WORKSPACE_ROOT / ".claude" / "skills" / "wm-do-thing"
    assert fs.is_symlink(skill_link)


# ── extension = false ─────────────────────────────────────────────────────────


def _opted_out_standalone_workspace(*, ref: str | None = None) -> tuple[WorkspaceConfig, FakeFilesystem, dict, Path]:
    """A cloned standalone that ships a winter-ext.toml, skills, an agent, and an index.md — but opts out."""
    ext_path = WORKSPACE_ROOT / "corpus"
    skill_dir = ext_path / "skills" / "do-thing"
    agents_dir = ext_path / "agents"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git", ext_path, skill_dir, agents_dir])
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    fs.files[ext_path / "index.md"] = "# corpus\n"
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    fs.files[agents_dir / "reviewer.md"] = (
        "---\nname: reviewer\ndescription: Reviews code\nmodel: sonnet\n---\nYou review code.\n"
    )
    config_files: dict = {manifest_path: {"name": "corpus", "prefix": "co"}}
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        standalone_repos=[
            StandaloneRepositoryConfig(name="corpus", url="git@example.com:org/corpus.git", extension=False, ref=ref),
        ],
    )
    return cfg, fs, config_files, ext_path


def test_reconcile_standalones_projects_nothing_for_a_standalone_declaring_extension_false(
    init_reporter: FakeInitReporter,
) -> None:
    cfg, fs, config_files, _ = _opted_out_standalone_workspace()

    svc = _service_with_ext_and_agents(cfg, fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert not fs.is_symlink(WORKSPACE_ROOT / ".claude" / "skills" / "co-do-thing")
    assert WORKSPACE_ROOT / ".claude" / "agents" / "co-reviewer.md" not in fs.files
    assert WORKSPACE_ROOT / "AGENTS.winter.md" not in fs.files


def test_reconcile_standalones_still_clones_and_pins_a_standalone_declaring_extension_false(
    init_reporter: FakeInitReporter,
) -> None:
    """The repo is data, not nothing: clone and `ref` pinning (with its lock entry) still apply."""
    cfg, _, config_files, ext_path = _opted_out_standalone_workspace(ref="develop")
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT])  # not cloned yet
    git = FakeGitRepository()
    git.clean_worktrees.add(ext_path)
    git.resolved_refs[(ext_path, "develop")] = (RefKind.branch, "d" * 40)
    lock_repo = FakeConfigLockRepository()

    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files))
    svc = InitService(
        config=cfg,
        repo_factory=RepositoryFactory(cfg, fs=fs),
        extension_symlink_svc=ExtensionSymlinkService(config=cfg, fs=fs, manifest_loader=manifest_loader),
        extension_hook_svc=ExtensionHookService(
            config=cfg, fs=fs, subprocess_runner=FakeSubprocessRunner(), manifest_loader=manifest_loader
        ),
        extension_exclude_svc=ExtensionExcludeService(
            config=cfg,
            fs=fs,
            manifest_loader=manifest_loader,
            exclude_locator=FakeWorkspaceExcludeLocator(fs, cfg.workspace_root),
        ),
        extension_agentsmd_svc=ExtensionAgentsMdService(config=cfg, fs=fs, manifest_loader=manifest_loader),
        fs=fs,
        subprocess_runner=FakeSubprocessRunner(),
        git_repo=git,
        git_ops=GitOpsService(RepoErrorFactory()),
        registry=FakeEnvIndexRegistry(),
        config_lock_repo=lock_repo,
        exclude_locator=FakeWorkspaceExcludeLocator(fs, cfg.workspace_root),
    )

    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert git.clones == [("git@example.com:org/corpus.git", ext_path)]
    assert git.branch_checkouts == [(ext_path, "develop")]
    assert lock_repo.write_calls[0]["corpus"].commit == "d" * 40


def test_reconcile_standalones_prunes_projections_left_before_extension_false(
    init_reporter: FakeInitReporter,
) -> None:
    """Skills and agents an earlier `ws init` projected are retracted; the clone itself stays."""
    cfg, fs, config_files, ext_path = _opted_out_standalone_workspace()
    earlier = cfg.model_copy(
        update={
            "standalone_repos": [cfg.standalone_repos[0].model_copy(update={"extension": True})],
        }
    )
    projecting = _service_with_ext_and_agents(earlier, fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    assert projecting.reconcile_standalones(FakeInitReporter()) is True
    skill_link = WORKSPACE_ROOT / ".claude" / "skills" / "co-do-thing"
    agent_copy = WORKSPACE_ROOT / ".claude" / "agents" / "co-reviewer.md"
    assert fs.is_symlink(skill_link)
    assert agent_copy in fs.files
    assert WORKSPACE_ROOT / "AGENTS.winter.md" in fs.files

    svc = _service_with_ext_and_agents(cfg, fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert not fs.is_symlink(skill_link)
    assert agent_copy not in fs.files
    assert WORKSPACE_ROOT / "AGENTS.winter.md" not in fs.files
    assert fs.is_file(ext_path / "index.md")


def test_reconcile_standalones_keeps_a_path_only_exclude_for_an_extension_false_standalone(
    init_reporter: FakeInitReporter,
) -> None:
    """The clone stays out of the workspace's `git status`, with none of the projection globs."""
    cfg, fs, config_files, _ = _opted_out_standalone_workspace()

    svc = _service_with_ext_and_agents(cfg, fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    assert svc.reconcile_standalones(init_reporter) is True

    exclude = fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"]
    assert "/corpus/" in exclude
    assert ".claude/skills/co-" not in exclude


def test_reconcile_standalones_retracts_a_manifest_bearing_project_repo_declaring_extension_false(
    init_reporter: FakeInitReporter,
) -> None:
    ext_path = WORKSPACE_ROOT / "projects" / "winter-product"
    skill_dir = ext_path / "skills" / "do-thing"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", ext_path, skill_dir])
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    fs.files[ext_path / "index.md"] = "# product\n"
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    config_files: dict = {manifest_path: {"name": "winter-product", "prefix": "wp"}}

    def config(extension: bool) -> WorkspaceConfig:
        return WorkspaceConfig(
            workspace_root=WORKSPACE_ROOT,
            service_prefix="t",
            main_branch="main",
            adopt_extensions=AdoptExtensions.winter,
            git_identity=None,
            project_repos=[
                ProjectRepositoryConfig(
                    name="winter-product", url="git@example.com:org/winter-product.git", extension=extension
                ),
            ],
        )

    first = _service_with_ext_and_agents(config(True), fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    assert first.reconcile_standalones(FakeInitReporter()) is True
    skill_link = WORKSPACE_ROOT / ".claude" / "skills" / "wp-do-thing"
    assert fs.is_symlink(skill_link)
    assert "winter-product" in fs.files[WORKSPACE_ROOT / "AGENTS.winter.md"]

    second = _service_with_ext_and_agents(config(False), fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    ok = second.reconcile_standalones(init_reporter)

    assert ok is True
    assert not fs.is_symlink(skill_link)
    assert WORKSPACE_ROOT / "AGENTS.winter.md" not in fs.files
    assert fs.is_file(ext_path / "index.md")


# ── Upstream inference tests ──────────────────────────────────────────────────


def _two_repo_config() -> WorkspaceConfig:
    """WorkspaceConfig with two non-pinned repos (existing + newly-added)."""
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="alpha-repo", url="git@example.com:org/alpha-repo.git"),
            ProjectRepositoryConfig(name="beta-repo", url="git@example.com:org/beta-repo.git"),
        ],
    )


def _pinned_two_repo_config() -> WorkspaceConfig:
    """WorkspaceConfig with one pinned repo and one non-pinned repo."""
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="alpha-repo", url="git@example.com:org/alpha-repo.git", pinned=True),
            ProjectRepositoryConfig(name="beta-repo", url="git@example.com:org/beta-repo.git"),
        ],
    )


def test_reconcile_env_infers_upstream_for_newly_added_repo(
    init_reporter: FakeInitReporter,
) -> None:
    """Newly-added repo gains the inferred upstream when siblings share a single consistent upstream.

    Scenario: env 'myenv' already has 'alpha-repo' worktree tracking 'origin/master'.
    'beta-repo' is newly added — its worktree does not exist yet. After reconcile,
    'beta-repo' should be connected to 'origin/master' inferred from 'alpha-repo'.
    """
    cfg = _two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            # alpha-repo worktree already exists; beta-repo is absent (newly added)
            alpha_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    # alpha-repo worktree already has an upstream
    git.tracking_branches[alpha_worktree] = "origin/master"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # beta-repo worktree was created
    assert any(wt == beta_main for wt, _, _, _ in git.added_worktrees)
    # upstream inferred and set for beta-repo
    assert (beta_worktree, "origin/master") in git.upstreams_set
    assert beta_worktree in git.push_default_set
    # reporter recorded the auto-connect action
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert len(auto_connect_actions) == 1
    assert auto_connect_actions[0][0] == "beta-repo"
    assert auto_connect_actions[0][3] == "origin/master"
    # alpha-repo upstream left unchanged (was already set)
    alpha_upstreams = [(p, ref) for p, ref in git.upstreams_set if p == alpha_worktree]
    assert alpha_upstreams == []


def test_reconcile_env_infers_upstream_despite_born_with_incidental_tracking(
    init_reporter: FakeInitReporter,
) -> None:
    """Regression for #148: a newly-created branch born auto-tracking is still wired to the inferred upstream.

    Simulates a branch that comes out of `add_worktree` already reporting a
    tracking branch — e.g. what `git worktree add -b <branch> <base>` would do
    under `branch.autoSetupMerge = always`, or when `<base>` is itself a
    remote-tracking ref, before the `--no-track` fix. Even though the fake
    reports a non-None (and wrong) tracking branch for the just-created
    worktree, init must still apply the env's inferred upstream rather than
    bailing out on the stale "already has an upstream" guard.
    """
    cfg = _two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            # alpha-repo worktree already exists; beta-repo is absent (newly added)
            alpha_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    # alpha-repo worktree already has an upstream — the env's shared upstream.
    git.tracking_branches[alpha_worktree] = "origin/master"
    # beta-repo's worktree doesn't exist yet, but once "created" it is born
    # with an incidental upstream unrelated to the env (a local branch, as
    # `autoSetupMerge = always` would produce from a local base branch).
    git.tracking_branches[beta_worktree] = "main"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # beta-repo worktree was created
    assert any(wt == beta_main for wt, _, _, _ in git.added_worktrees)
    # inferred upstream still applied, overriding the incidental born-with tracking
    assert (beta_worktree, "origin/master") in git.upstreams_set
    assert beta_worktree in git.push_default_set
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert len(auto_connect_actions) == 1
    assert auto_connect_actions[0][0] == "beta-repo"
    assert auto_connect_actions[0][3] == "origin/master"


def test_reconcile_env_leaves_repo_unconnected_when_siblings_diverge(
    init_reporter: FakeInitReporter,
) -> None:
    """Divergent sibling upstreams → newly-added repo left unconnected.

    Scenario: env has 'alpha-repo' on 'origin/master' and 'gamma-repo' on 'origin/develop'.
    A newly-added 'beta-repo' cannot infer a consensus upstream, so it is left unconnected.
    """
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        project_repos=[
            ProjectRepositoryConfig(name="alpha-repo", url="git@example.com:org/alpha-repo.git"),
            ProjectRepositoryConfig(name="beta-repo", url="git@example.com:org/beta-repo.git"),
            ProjectRepositoryConfig(name="gamma-repo", url="git@example.com:org/gamma-repo.git"),
        ],
    )
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    gamma_main = WORKSPACE_ROOT / "projects" / "gamma-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"
    gamma_worktree = WORKSPACE_ROOT / "myenv" / "gamma-repo"

    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            gamma_main,
            alpha_worktree,
            gamma_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    git.local_branches[gamma_main] = ["myenv"]
    # siblings have divergent upstreams → no consensus
    git.tracking_branches[alpha_worktree] = "origin/master"
    git.tracking_branches[gamma_worktree] = "origin/develop"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # beta-repo worktree was created
    assert any(wt == beta_main for wt, _, _, _ in git.added_worktrees)
    # no upstream set for beta-repo (divergent siblings → leave unconnected)
    beta_upstreams = [(p, ref) for p, ref in git.upstreams_set if p == beta_worktree]
    assert beta_upstreams == []
    assert beta_worktree not in git.push_default_set
    # no upstream_inferred action reported
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert auto_connect_actions == []


def test_reconcile_env_pinned_repo_not_touched_by_inference(
    init_reporter: FakeInitReporter,
) -> None:
    """Pinned repo is never touched by upstream inference — its tracking is owned by _configure_pinned_tracking."""
    cfg = _pinned_two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    # Both worktrees already exist; beta has an upstream, alpha (pinned) does not
    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            alpha_worktree,
            beta_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["myenv"]
    # alpha-repo is pinned — no upstream (inference must not touch it)
    git.tracking_branches[alpha_worktree] = None
    # beta-repo has an upstream — serves as the only sibling
    git.tracking_branches[beta_worktree] = "origin/master"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # No upstream_inferred action at all (alpha is pinned, beta already has one)
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert auto_connect_actions == []
    # Inference must not have reported an upstream_inferred action for alpha-repo (pinned)
    inferred_for_alpha = [a for a in init_reporter.actions if a[2] == "upstream_inferred" and a[0] == "alpha-repo"]
    assert inferred_for_alpha == []


def test_reconcile_env_pinned_newly_added_repo_not_connected_by_inference(
    init_reporter: FakeInitReporter,
) -> None:
    """Newly-added repo that is itself pinned is not touched by upstream inference.

    The pinned guard in _connect_inferred_upstream fires for the target repo itself,
    leaving _configure_pinned_tracking as the sole owner of that worktree's upstream.
    """
    cfg = _pinned_two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    # beta-repo (non-pinned) worktree already exists with an upstream.
    # alpha-repo (pinned) worktree is absent — it is the "newly-added" repo.
    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            beta_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["main"]
    git.local_branches[beta_main] = ["myenv"]
    # beta-repo (non-pinned) already tracks origin/master — provides the inferred upstream
    git.tracking_branches[beta_worktree] = "origin/master"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # alpha-repo worktree was created (it was newly added)
    alpha_main_path = WORKSPACE_ROOT / "projects" / "alpha-repo"
    assert any(wt == alpha_main_path for wt, _, _, _ in git.added_worktrees)
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    # upstream_inferred must NOT have fired for alpha-repo (pinned guard must return early)
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred" and a[0] == "alpha-repo"]
    assert auto_connect_actions == [], "upstream_inferred must not fire for a pinned repo"
    # _configure_pinned_tracking still fires and sets the correct pinned upstream
    pinned_actions = [a for a in init_reporter.actions if a[2] == "pinned_tracking_set" and a[0] == "alpha-repo"]
    assert len(pinned_actions) == 1
    # the upstream set for alpha must be the pinned ref (origin/<main-branch>), not the inferred one
    alpha_upstreams = [(p, ref) for p, ref in git.upstreams_set if p == alpha_worktree]
    assert alpha_upstreams == [(alpha_worktree, "origin/main")]


def test_reconcile_env_already_connected_repo_unchanged(
    init_reporter: FakeInitReporter,
) -> None:
    """A worktree that already has an upstream is left unchanged (idempotent re-run)."""
    cfg = _two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    # Both worktrees already exist and both already have upstreams
    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            alpha_worktree,
            beta_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["myenv"]
    git.tracking_branches[alpha_worktree] = "origin/master"
    git.tracking_branches[beta_worktree] = "origin/master"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # No upstreams set during this reconcile (both already connected)
    assert git.upstreams_set == []
    assert git.push_default_set == []
    # No upstream_inferred action
    auto_connect_actions = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert auto_connect_actions == []


def _connected_env_config_with_cmd() -> WorkspaceConfig:
    """Two non-pinned repos; the newly-added one carries a bootstrap `cmd` list."""
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="alpha-repo", url="git@example.com:org/alpha-repo.git"),
            ProjectRepositoryConfig(name="beta-repo", url="git@example.com:org/beta-repo.git", cmd=["direnv allow"]),
        ],
    )


def test_reconcile_env_wires_unpushed_inferred_upstream_and_runs_cmd(
    init_reporter: FakeInitReporter,
) -> None:
    """Regression for #156: connect to an unpushed feature branch, then init a fresh sibling repo.

    Scenario: env 'myenv' was connected to a feature branch that was never
    pushed, so its existing 'alpha-repo' worktree tracks `origin/feature/foo`
    (a remote-tracking ref git cannot resolve locally). A newly-added
    'beta-repo' with a bootstrap `cmd` list is reconciled. Init must wire the
    inferred `origin/feature/foo` upstream tolerantly (no git-128 / RepoError)
    AND still run beta-repo's `cmd` list.
    """
    cfg = _connected_env_config_with_cmd()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            alpha_worktree,  # beta-repo absent → newly added this run
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    # The env is connected to a feature branch that was never pushed.
    git.tracking_branches[alpha_worktree] = "origin/feature/foo"

    subprocess = FakeSubprocessRunner(popen_responses={"direnv allow": (["trusted"], 0)})
    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is True
    # No error was reported for the newly-added repo.
    assert init_reporter.errors == []
    # Upstream tracking was set to the unpushed origin/<feature> ref.
    assert (beta_worktree, "origin/feature/foo") in git.upstreams_set
    inferred = [a for a in init_reporter.actions if a[2] == "upstream_inferred"]
    assert inferred and inferred[0][0] == "beta-repo" and inferred[0][3] == "origin/feature/foo"
    # The repo's cmd list executed.
    assert ("beta-repo", "direnv allow", 0) in init_reporter.cmds_completed


class _UpstreamRefusingGitRepository(FakeGitRepository):
    """FakeGitRepository whose set_upstream_to raises — mimics the strict git-128 path.

    Exercises the cmd-isolation backstop: even if upstream wiring blows up, the
    repo's `cmd` bootstrap must still run — but the repo (and therefore the env)
    is still marked failed; wiring failures are real per-repo failures, not
    silently downgraded to a soft skip.
    """

    def set_upstream_to(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> None:
        raise RepoError(f"set-upstream-to {ref} failed at {path}", cwd=str(path))


def test_reconcile_env_upstream_failure_does_not_skip_cmd(
    init_reporter: FakeInitReporter,
) -> None:
    """A failure wiring upstream is isolated from the cmd bootstrap but still fails the repo.

    Even when `set_upstream_to` raises, the repo's `cmd` list still runs — the
    wiring failure doesn't short-circuit it — but the repo, and therefore the
    env, is still reported as failed: the failure surfaces via `reporter.repo_error`
    and `reconcile_env` returns `False`, so a wiring failure like a detached-HEAD
    worktree is never silently swallowed.
    """
    cfg = _connected_env_config_with_cmd()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"

    fs = FakeFilesystem(
        directories=[
            WORKSPACE_ROOT / "projects",
            alpha_main,
            beta_main,
            alpha_worktree,
        ]
    )
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    git = _UpstreamRefusingGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    git.tracking_branches[alpha_worktree] = "origin/feature/foo"

    subprocess = FakeSubprocessRunner(popen_responses={"direnv allow": (["trusted"], 0)})
    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is False
    # Wiring failure is reported as a real repo error naming the repo.
    beta_errors = [msg for repo, msg in init_reporter.errors if repo == "beta-repo"]
    assert beta_errors and "set-upstream-to" in beta_errors[0]
    # The cmd list ran regardless of the upstream failure.
    assert ("beta-repo", "direnv allow", 0) in init_reporter.cmds_completed


# ── _run_per_repo: unexpected-exception backstop ───────────────────────────


class _UnexpectedlyExplodingGitRepository(FakeGitRepository):
    """`set_user_identity` raises a raw, non-`RepoError` exception for one path.

    Neither `_reconcile_worktree_repo`'s `except (RepoError, OSError)` nor
    `_wire_upstream_tracking`'s `except RepoError` catches this — it stands in
    for a genuinely unexpected bug in a leaf git operation, the case
    `_run_per_repo`'s backstop exists for.
    """

    def __init__(self, exploding_path: Path) -> None:
        super().__init__()
        self._exploding_path = exploding_path

    def set_user_identity(self, path: Path, name: str, email: str, *, repo_name: str, env: str | None) -> None:
        if path == self._exploding_path:
            raise ValueError(f"unexpected identity failure at {path}")
        super().set_user_identity(path, name, email, repo_name=repo_name, env=env)


def test_run_per_repo_reports_unexpected_exception_without_unwinding(
    init_reporter: FakeInitReporter,
) -> None:
    """A repo whose leaf work raises an unanticipated exception fails only that
    repo — `_run_per_repo` catches it as a backstop, reports it via
    `reporter.repo_error`, and the sibling repo in the same env still reconciles."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="alpha-repo", url="git@example.com:org/alpha-repo.git"),
            ProjectRepositoryConfig(name="beta-repo", url="git@example.com:org/beta-repo.git"),
        ],
    )
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"

    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", alpha_main, beta_main, alpha_worktree, beta_worktree])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = _UnexpectedlyExplodingGitRepository(exploding_path=alpha_worktree)

    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_env("myenv", init_reporter)

    assert ok is False
    alpha_errors = [msg for repo, msg in init_reporter.errors if repo == "alpha-repo"]
    assert alpha_errors and "unexpected" in alpha_errors[0]
    # The sibling repo's identity was still applied — the exploding repo's
    # failure did not unwind the fan-out.
    assert (beta_worktree, "Bot", "bot@example.com") in git.identities


# ── reconcile_env: unexpected-exception backstop at the env level ──────────


class _UnexpectedlyExplodingUpstreamInferenceGitRepository(FakeGitRepository):
    """`get_tracking_branch` raises a raw, non-`RepoError` exception for one worktree path.

    Stands in for an unguarded `git.InvalidGitRepositoryError` reaching
    `_infer_env_upstream` — a call that happens in `reconcile_env`'s env-level
    body *before* `_run_per_repo`'s own per-repo backstop is even reached, so
    this exercises `reconcile_env`'s own env-level guard instead.
    """

    def __init__(self, exploding_path: Path) -> None:
        super().__init__()
        self._exploding_path = exploding_path

    def get_tracking_branch(self, path: Path, *, repo_name: str, env: str | None) -> str | None:
        if path == self._exploding_path:
            raise ValueError(f"not a git repository: {path}")
        return super().get_tracking_branch(path, repo_name=repo_name, env=env)


def test_reconcile_env_env_level_exception_does_not_unwind_reconcile_all(
    init_reporter: FakeInitReporter,
) -> None:
    """An unexpected exception raised outside `_run_per_repo` — e.g. from
    `_infer_env_upstream`, called directly in `reconcile_env`'s own body — fails
    only that env. `reconcile_env`'s own backstop catches and reports it instead
    of letting it unwind `reconcile_all`'s per-env loop, so a later env still
    reconciles."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
    )
    demo_source = WORKSPACE_ROOT / "projects" / "demo"
    alpha_worktree = WORKSPACE_ROOT / "alpha" / "demo"
    beta_worktree = WORKSPACE_ROOT / "beta" / "demo"

    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_source, alpha_worktree, beta_worktree])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()

    git = _UnexpectedlyExplodingUpstreamInferenceGitRepository(exploding_path=alpha_worktree)
    # `_discover_existing_worktrees` reads `git worktree list` on the source
    # checkout — alpha is ordered first so its failure is encountered before beta.
    git.worktree_paths[demo_source] = [demo_source, alpha_worktree, beta_worktree]
    # beta's own worktree already has an upstream so its inference finds a single
    # agreeing sibling and reconciles cleanly despite alpha's env-level crash.
    git.tracking_branches[beta_worktree] = "origin/main"

    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_all(init_reporter)

    assert ok is False
    # alpha's env-level exception was caught and reported by reconcile_env's own
    # backstop, not left to unwind reconcile_all's per-env loop.
    alpha_errors = [msg for repo, msg in init_reporter.errors if repo == "alpha"]
    assert alpha_errors and "unexpected" in alpha_errors[0], init_reporter.errors
    assert ("alpha", False) in init_reporter.targets_completed
    # beta still reconciled despite alpha's env-level crash: its own worktree
    # already agreed with its (single-repo) inferred upstream, so no tracking
    # write was needed, but its identity step still ran to completion.
    assert (beta_worktree, "Bot", "bot@example.com") in git.identities
    assert ("beta", True) in init_reporter.targets_completed


# ── reconcile_all: detached HEAD in one env does not skip later envs ───────


class _DetachedHeadGitRepository(FakeGitRepository):
    """`set_upstream_to` raises `RepoError` for one specific worktree path.

    Stands in for the post-fix `GitPythonRepository.set_upstream_to` on a
    genuinely detached-HEAD worktree — a `RepoError` naming the worktree path,
    not the uncaught `TypeError` this behavior replaces.
    """

    def __init__(self, detached_path: Path) -> None:
        super().__init__()
        self._detached_path = detached_path

    def set_upstream_to(self, path: Path, ref: str, *, repo_name: str, env: str | None) -> None:
        if path == self._detached_path:
            raise RepoError(f"set-upstream-to {ref} failed at {path}: HEAD is detached", cwd=str(path))
        super().set_upstream_to(path, ref, repo_name=repo_name, env=env)


def test_reconcile_all_continues_past_detached_head_repo_in_earlier_env(
    init_reporter: FakeInitReporter,
) -> None:
    """A detached-HEAD worktree in one env fails that repo — naming the env/repo
    via the worktree path — but every other env still reconciles and
    `reconcile_all` exits with `False` rather than crashing partway through."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=GitIdentity(name="Bot", email="bot@example.com"),
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git", pinned=True),
        ],
    )
    demo_source = WORKSPACE_ROOT / "projects" / "demo"
    alpha_worktree = WORKSPACE_ROOT / "alpha" / "demo"
    beta_worktree = WORKSPACE_ROOT / "beta" / "demo"

    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_source, alpha_worktree, beta_worktree])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()

    git = _DetachedHeadGitRepository(detached_path=alpha_worktree)
    # `_discover_existing_worktrees` reads `git worktree list` on the source
    # checkout — "alpha" sorts before "beta" so alpha's failure is encountered
    # first in the per-env loop.
    git.worktree_paths[demo_source] = [demo_source, alpha_worktree, beta_worktree]

    svc = _service(cfg, fs, subprocess, git)
    ok = svc.reconcile_all(init_reporter)

    assert ok is False
    # alpha's failure names the repo and carries its worktree path (env + repo).
    alpha_errors = [msg for repo, msg in init_reporter.errors if repo == "demo" and str(alpha_worktree) in msg]
    assert alpha_errors, init_reporter.errors
    assert ("alpha", False) in init_reporter.targets_completed
    # beta still reconciled: its worktree's tracking was wired despite alpha's failure.
    assert (beta_worktree, "origin/main") in git.upstreams_set
    assert ("beta", True) in init_reporter.targets_completed


def test_reconcile_env_names_the_env_on_worktree_calls_and_none_on_source_checkout_calls(
    init_reporter: FakeInitReporter,
) -> None:
    """Every git call init makes on a feature worktree names its env; the source checkout's names none.

    Every call, worktree or source checkout, names the repo it acts for.
    """
    cfg = _two_repo_config()
    alpha_main = WORKSPACE_ROOT / "projects" / "alpha-repo"
    beta_main = WORKSPACE_ROOT / "projects" / "beta-repo"
    alpha_worktree = WORKSPACE_ROOT / "myenv" / "alpha-repo"
    beta_worktree = WORKSPACE_ROOT / "myenv" / "beta-repo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", alpha_main, beta_main, alpha_worktree])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    git = FakeGitRepository()
    git.local_branches[alpha_main] = ["myenv"]
    git.local_branches[beta_main] = ["main"]
    git.tracking_branches[alpha_worktree] = "origin/master"

    svc = _service(cfg, fs, FakeSubprocessRunner(), git)
    assert svc.reconcile_env("myenv", init_reporter) is True

    assert git.env_calls
    for method, path, env in git.env_calls:
        in_env = path in (alpha_worktree, beta_worktree)
        assert env == ("myenv" if in_env else None), f"{method} on {path} was given env={env!r}"
    # The calls that acted on the new worktree were made: creating it, inferring and wiring tracking, identity.
    for method in ("add_worktree", "set_upstream_to", "set_push_default_upstream", "set_user_identity"):
        assert "myenv" in git.envs_for(method), method
    assert git.envs_for("get_local_branches") == [None]
    owner = {alpha_main: "alpha-repo", alpha_worktree: "alpha-repo", beta_main: "beta-repo", beta_worktree: "beta-repo"}
    assert git.repo_name_calls
    for method, path, repo_name in git.repo_name_calls:
        assert repo_name == owner[path], f"{method} on {path} was given repo_name={repo_name!r}"


def test_reconcile_projects_and_standalones_name_no_env(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """A source checkout and a standalone are not feature worktrees: init's git calls on them name no env."""
    ext_path = WORKSPACE_ROOT / "my-ext"
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path, ext_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    cfg = _standalone_config(ref="v1.0.0")
    lock_repo = FakeConfigLockRepository(
        entries={"my-ext": LockEntry(name="my-ext", ref="v1.0.0", kind=RefKind.tag, commit=STANDALONE_SHA)}
    )
    git = FakeGitRepository()

    svc = _service(cfg, fs, FakeSubprocessRunner(), git, config_lock_repo=lock_repo)
    svc.reconcile_projects(init_reporter)
    svc.reconcile_standalones(init_reporter)

    assert git.env_calls
    assert {env for _method, _path, env in git.env_calls} == {None}
    # Only the pinned standalone runs git here (no identity is configured); its calls name it.
    assert {(path, repo_name) for _method, path, repo_name in git.repo_name_calls} == {(ext_path, "my-ext")}


def test_reconcile_standalones_keeps_a_live_extensions_projection_when_an_extension_false_mirror_shares_its_prefix(
    init_reporter: FakeInitReporter,
) -> None:
    """A mirror cloned as data under a live extension's prefix must not retract that extension, on any run."""
    live_path = WORKSPACE_ROOT / "my-ext"
    mirror_path = WORKSPACE_ROOT / "mirrors" / "my-ext"
    skill_dir = live_path / "skills" / "do-thing"
    agents_dir = live_path / "agents"
    mirror_skill_dir = mirror_path / "skills" / "do-thing"
    fs = FakeFilesystem(
        directories=[live_path, skill_dir, agents_dir, mirror_path, mirror_skill_dir],
    )
    live_manifest = live_path / "winter-ext.toml"
    mirror_manifest = mirror_path / "winter-ext.toml"
    fs.files[live_manifest] = ""
    fs.files[mirror_manifest] = ""
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    fs.files[mirror_skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    fs.files[agents_dir / "reviewer.md"] = (
        "---\nname: reviewer\ndescription: Reviews code\nmodel: sonnet\n---\nYou review code.\n"
    )
    config_files: dict = {live_manifest: {"name": "my-ext", "prefix": "me"}, mirror_manifest: {"prefix": "me"}}
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        git_identity=None,
        standalone_repos=[
            StandaloneRepositoryConfig(name="my-ext", url="git@example.com:org/my-ext.git"),
            StandaloneRepositoryConfig(
                name="my-ext-mirror",
                url="git@example.com:org/my-ext-mirror.git",
                path="mirrors/my-ext",
                extension=False,
            ),
        ],
    )
    skill_link = WORKSPACE_ROOT / ".claude" / "skills" / "me-do-thing"
    agent_copy = WORKSPACE_ROOT / ".claude" / "agents" / "me-reviewer.md"

    for _ in range(3):
        svc = _service_with_ext_and_agents(cfg, fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
        assert svc.reconcile_standalones(init_reporter) is True
        assert fs.is_symlink(skill_link)
        assert agent_copy in fs.files

    # The live extension's source is what the link resolves to, never the mirror's.
    assert fs.symlinks[skill_link].as_posix().endswith("my-ext/skills/do-thing")
    assert "mirrors" not in fs.symlinks[skill_link].as_posix()


def test_reconcile_standalones_renders_a_routing_row_for_a_manifest_less_project_repo_declaring_load(
    init_reporter: FakeInitReporter,
) -> None:
    repo_path = WORKSPACE_ROOT / "projects" / "app"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", repo_path])
    fs.files[repo_path / "AGENTS.md"] = "# app\n"
    skill_dir = repo_path / "skills" / "do-thing"
    fs.directories.add(skill_dir)
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.all,
        git_identity=None,
        project_repos=[
            ProjectRepositoryConfig(name="app", url="git@example.com:org/app.git", load=ExtensionLoad.lazy),
        ],
    )

    svc = _service_with_ext_and_agents(cfg, fs, {}, FakeSubprocessRunner(), FakeGitRepository())
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert "- **app**: `<env>/app/AGENTS.md`" in fs.files[WORKSPACE_ROOT / "AGENTS.winter.md"]
    # Context delivery only: nothing is projected for it, even under `adopt_extensions = "all"`.
    assert not fs.is_symlink(WORKSPACE_ROOT / ".claude" / "skills" / "app-do-thing")


# ── nested workspaces ─────────────────────────────────────────────────────────

LAB_MAIN = WORKSPACE_ROOT / "projects" / "lab"
LAB_ALPHA = WORKSPACE_ROOT / "alpha" / "lab"
TRUST = "./bootstrap.sh trust"


def _nested_config(*, cmd: list[str] | None = None) -> WorkspaceConfig:
    # `nested = true` parses to `extension = false` too; mirror that here.
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(
                name="lab", url="git@example.com:org/lab.git", cmd=cmd or [], nested=True, extension=False
            ),
        ],
    )


def _nested_fs() -> FakeFilesystem:
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", LAB_MAIN, WORKSPACE_ROOT / ".git" / "info"])
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    return fs


class _LoggingSubprocessRunner(FakeSubprocessRunner):
    """FakeSubprocessRunner that also appends each streamed command to a shared event log."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events = events

    def popen(self, cmd: Any, **kwargs: Any) -> Any:
        self._events.append(f"cmd:{cmd}")
        return super().popen(cmd, **kwargs)


def test_reconcile_env_runs_nested_init_in_the_worktree_after_cmd(init_reporter: FakeInitReporter) -> None:
    events: list[str] = []
    subprocess = _LoggingSubprocessRunner(events, popen_responses={TRUST: ([], 0)})
    runner = FakeNestedWorkspaceRunner(events=events, init_lines=["✓ projects/ done"])
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]

    svc = _service(_nested_config(cmd=[TRUST]), _nested_fs(), subprocess, git, nested_svc=make_nested_service(runner))
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is True
    assert events == [f"cmd:{TRUST}", f"nested_init:{LAB_ALPHA}"]
    assert runner.calls == [("init", LAB_ALPHA)]
    assert ("lab", "winter ws init", 0) in init_reporter.cmds_completed
    assert ("lab", "✓ projects/ done") in init_reporter.cmd_output


def test_reconcile_env_runs_a_nested_repos_cmd_without_the_outer_workspace_env(
    init_reporter: FakeInitReporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/outer/.venv")
    monkeypatch.setenv("PATH", "/outer/.venv/bin:/usr/bin")
    monkeypatch.setenv("WINTER_PORT_BASE", "4020")
    monkeypatch.setenv("WINTER_NESTED_CHAIN", "/up")
    monkeypatch.setenv("WINTER_LOG_LEVEL", "debug")
    subprocess = FakeSubprocessRunner(popen_responses={TRUST: ([], 0)})
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]

    svc = _service(
        _nested_config(cmd=[TRUST]),
        _nested_fs(),
        subprocess,
        git,
        nested_svc=make_nested_service(FakeNestedWorkspaceRunner()),
    )
    assert svc.reconcile_env("alpha", init_reporter) is True

    env = subprocess.popen_envs[0]
    assert "VIRTUAL_ENV" not in env
    assert "WINTER_PORT_BASE" not in env
    assert "WINTER_NESTED_CHAIN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["WINTER_LOG_LEVEL"] == "debug"
    assert env["CI"] == "1"


def test_reconcile_env_fails_when_nested_init_fails(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner(init_returncode=2)
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]

    svc = _service(_nested_config(), _nested_fs(), FakeSubprocessRunner(), git, nested_svc=make_nested_service(runner))
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is False
    assert ("alpha", False) in init_reporter.targets_completed
    assert any(repo == "lab" and "winter ws init" in msg and "code 2" in msg for repo, msg in init_reporter.errors)


def test_reconcile_env_fails_and_skips_nested_init_when_the_footprint_exceeds_the_outer_band(
    init_reporter: FakeInitReporter,
) -> None:
    runner = FakeNestedWorkspaceRunner()
    overlay = FakeLocalOverlayRepository()
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]
    narrow = make_nested_service(runner, config=make_workspace_config(ports_per_env=20), overlay=overlay)

    svc = _service(_nested_config(), _nested_fs(), FakeSubprocessRunner(), git, nested_svc=narrow)
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is False
    assert runner.calls == []
    assert overlay.upserts == []
    assert any(repo == "lab" and "980 ports" in msg and "holds only 10" in msg for repo, msg in init_reporter.errors)


def test_reconcile_env_reports_a_malformed_nested_config_as_the_repos_error(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()
    overlay = FakeLocalOverlayRepository()
    overlay.malformed.add(LAB_ALPHA)
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]

    svc = _service(
        _nested_config(),
        _nested_fs(),
        FakeSubprocessRunner(),
        git,
        nested_svc=make_nested_service(runner, overlay=overlay),
    )
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is False
    assert runner.calls == []
    [(repo, message)] = init_reporter.errors
    assert repo == "lab"
    assert message.startswith(f"cannot delegate to the nested workspace at {LAB_ALPHA}: reading ")
    assert "config.local.toml" in message


def test_reconcile_env_skips_nested_init_when_cmd_fails(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()
    git = FakeGitRepository()
    git.local_branches[LAB_MAIN] = ["main"]
    subprocess = FakeSubprocessRunner(popen_responses={TRUST: ([], 1)})

    svc = _service(_nested_config(cmd=[TRUST]), _nested_fs(), subprocess, git, nested_svc=make_nested_service(runner))
    ok = svc.reconcile_env("alpha", init_reporter)

    assert ok is False
    assert runner.calls == []


def test_reconcile_projects_never_initializes_the_nested_source_checkout(init_reporter: FakeInitReporter) -> None:
    runner = FakeNestedWorkspaceRunner()
    subprocess = FakeSubprocessRunner(popen_responses={TRUST: ([], 0)})

    svc = _service(
        _nested_config(cmd=[TRUST]),
        _nested_fs(),
        subprocess,
        FakeGitRepository(),
        nested_svc=make_nested_service(runner),
    )
    ok = svc.reconcile_projects(init_reporter)

    assert ok is True
    assert runner.calls == []
    # The entry's `cmd` still runs in the source checkout, as for any project repo.
    assert subprocess.popen_calls == [(TRUST, LAB_MAIN)]


def test_reconcile_standalones_projects_nothing_for_a_nested_repo_with_agent_files(
    init_reporter: FakeInitReporter,
) -> None:
    """A nested workspace's root `AGENTS.md`, `winter-ext.toml`, and skills stay out of the outer workspace."""
    fs = _nested_fs()
    skill_dir = LAB_MAIN / "skills" / "do-thing"
    fs.directories.add(skill_dir)
    fs.files[LAB_MAIN / "AGENTS.md"] = "# Lab\n"
    fs.files[LAB_MAIN / "winter-ext.toml"] = ""
    fs.files[skill_dir / "SKILL.md"] = "---\ndescription: An example skill\n---\n\n# do-thing\n"
    config_files: dict = {LAB_MAIN / "winter-ext.toml": {"name": "lab", "prefix": "lab"}}

    svc = _service_with_ext_and_agents(_nested_config(), fs, config_files, FakeSubprocessRunner(), FakeGitRepository())
    ok = svc.reconcile_standalones(init_reporter)

    assert ok is True
    assert not fs.exists(WORKSPACE_ROOT / ".claude" / "skills" / "lab-do-thing")
    agents_winter = WORKSPACE_ROOT / "AGENTS.winter.md"
    assert agents_winter not in fs.files or "lab" not in fs.files[agents_winter]


def test_reconcile_standalones_strips_extension_block_when_last_repo_removed(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """With no repositories left, init still runs the exclude reconcile so stale blocks go away."""
    exclude_path = WORKSPACE_ROOT / ".git" / "info" / "exclude"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / ".git" / "info"])
    fs.files[exclude_path] = "# >>> gone-ext (managed by winter)\n/gone-ext/\n# <<< gone-ext\n"
    svc = _service(workspace_config, fs, FakeSubprocessRunner(), FakeGitRepository())

    assert svc.reconcile_standalones(init_reporter) is True

    assert "gone-ext" not in fs.files[exclude_path]
    assert "winter-config" in fs.files[exclude_path]


def test_reconcile_standalones_fails_on_malformed_excludes_declaration(init_reporter: FakeInitReporter) -> None:
    """A malformed `[excludes]` table makes the standalone reconcile fail, naming the extension."""
    ext_path = WORKSPACE_ROOT / "my-ext"
    manifest_path = ext_path / "winter-ext.toml"
    fs = FakeFilesystem(directories=[ext_path, WORKSPACE_ROOT / ".git" / "info"])
    fs.files[manifest_path] = ""
    config_files = {manifest_path: {"name": "my-ext", "excludes": {"paths": ["!/x"]}}}
    svc = _service_with_ext(_standalone_config(), fs, config_files, FakeSubprocessRunner(), FakeGitRepository())

    assert svc.reconcile_standalones(init_reporter) is False
    assert any(repo == "my-ext" and "excludes" in msg for repo, msg in init_reporter.errors)
