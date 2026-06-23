from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from tests.conftest import (
    FakeConfigFileReader,
    FakeConfigLockRepository,
    FakeEnvIndexRegistry,
    FakeFilesystem,
    FakeGitRepository,
    FakeInitReporter,
    FakeSubprocessRunner,
)
from winter_cli.config.models import (
    AdoptExtensions,
    GitIdentity,
    ProjectRepositoryConfig,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.workspace.agents_md_service import AgentsMdService
from winter_cli.modules.workspace.extension_claudemd_service import ExtensionClaudemdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.models.domain_model import LockEntry, RefKind
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
    git_ops: GitOpsService | None = None,
    config_lock_repo: FakeConfigLockRepository | None = None,
    registry: FakeEnvIndexRegistry | None = None,
) -> InitService:
    manifest_loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({}))
    return InitService(
        config=workspace_config,
        repo_factory=RepositoryFactory(workspace_config),
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
        ),
        extension_claudemd_svc=ExtensionClaudemdService(
            config=workspace_config,
            fs=fs,
        ),
        agents_md_svc=AgentsMdService(
            config=workspace_config,
            fs=fs,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=git_ops or GitOpsService(RepoErrorFactory()),
        registry=registry or FakeEnvIndexRegistry(),
        config_lock_repo=config_lock_repo,
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
    assert "WINTER_PORT_BASE=4020" in content
    # Workspace (index 0) port base is exposed alongside the env's own base.
    assert "WINTER_WORKSPACE_PORT_BASE=4000" in content
    # Identity applied to the worktree.
    assert (WORKSPACE_ROOT / "alpha" / "demo", "Bot", "bot@example.com") in git.identities


def test_reconcile_projects_seeds_workspace_env_and_excludes(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """reconcile_projects writes .winter.workspace.env (index-0 base) and excludes it + .winter/logs/."""
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
    # Workspace env file seeded at the root with the index-0 port base.
    ws_env = WORKSPACE_ROOT / ".winter.workspace.env"
    assert ws_env in fs.files
    ws_content = fs.files[ws_env]
    assert "WINTER_PORT_BASE=4000" in ws_content
    # Both workspace-root artifacts are git-excluded in one managed block.
    exclude = fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"]
    assert "# >>> winter-workspace/artifacts (managed by winter)" in exclude
    assert "/.winter.workspace.env" in exclude
    assert "/.winter/logs/" in exclude


def test_reconcile_projects_workspace_artifacts_idempotent_and_preserve_local(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Re-running leaves no duplicate blocks and preserves user vars below the marker."""
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git")
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""
    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()

    svc = _service(workspace_config, fs, subprocess, git)
    assert svc.reconcile_projects(init_reporter) is True

    # Append a user-managed var below the closing marker, then reconcile again.
    ws_env = WORKSPACE_ROOT / ".winter.workspace.env"
    fs.files[ws_env] = fs.files[ws_env] + "MY_LOCAL_VAR=keep\n"

    assert svc.reconcile_projects(init_reporter) is True

    ws_content = fs.files[ws_env]
    # User var preserved; managed block not duplicated.
    assert "MY_LOCAL_VAR=keep" in ws_content
    assert ws_content.count("WINTER_PORT_BASE=4000") == 1
    exclude = fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"]
    assert exclude.count("# >>> winter-workspace/artifacts (managed by winter)") == 1


def test_reconcile_env_uses_persisted_registry_index(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """_seed_winter_env calls EnvIndexAllocator.allocate, which returns the persisted
    slot for a known env rather than recomputing the suggested slot.

    A non-alias env name that is already in the registry (e.g. from a prior
    collision-probe run) must get back the same persisted index on every
    subsequent reconcile — not a freshly-suggested one that could differ.
    """
    demo_path = WORKSPACE_ROOT / "projects" / "demo"
    fs = FakeFilesystem(directories=[WORKSPACE_ROOT / "projects", demo_path])
    fs.directories.add(WORKSPACE_ROOT / ".git" / "info")
    fs.files[WORKSPACE_ROOT / ".git" / "info" / "exclude"] = ""

    subprocess = FakeSubprocessRunner()
    git = FakeGitRepository()
    git.local_branches[demo_path] = ["main"]

    # "myenv" is not in env_aliases so the allocator falls through to the
    # idempotent-existing-registration path. Pre-seed an out-of-band index
    # to prove the persisted value is returned, not the hash suggestion.
    # (Alias envs like "alpha" are always forced to their fixed slot by the
    # allocator — use a non-alias name here.)
    env_name = "myenv"
    persisted_index = 15  # arbitrary; not the hash suggestion for "myenv"
    registry = FakeEnvIndexRegistry(assignments={env_name: persisted_index})

    svc = _service(workspace_config, fs, subprocess, git, registry=registry)
    ok = svc.reconcile_env(env_name, init_reporter)

    assert ok is True
    env_file = WORKSPACE_ROOT / env_name / ".winter.env"
    content = fs.files[env_file]
    assert f"WINTER_ENV_INDEX={persisted_index}" in content
    # Port base = base_port + index * ports_per_env = 4000 + 15 * 20 = 4300
    assert "WINTER_PORT_BASE=4300" in content
    # Registry entry unchanged — allocate is idempotent for known entries.
    assert registry.assignments[env_name] == persisted_index


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
        session_prefix="t",
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

    def clone(self, url: str, dest: Path) -> None:  # type: ignore[override]
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
        repo_factory=RepositoryFactory(workspace_config),
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
        ),
        extension_claudemd_svc=ExtensionClaudemdService(
            config=workspace_config,
            fs=fs,
        ),
        agents_md_svc=AgentsMdService(
            config=workspace_config,
            fs=fs,
        ),
        fs=fs,
        subprocess_runner=subprocess,
        git_repo=git,
        git_ops=GitOpsService(RepoErrorFactory()),
        registry=FakeEnvIndexRegistry(),
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
        session_prefix="t",
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
        session_prefix="t",
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
        session_prefix="t",
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
