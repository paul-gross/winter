from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeFilesystem, FakeSubprocessRunner
from winter_cli.config.models import AdoptExtensions, ProjectRepositoryConfig, WorkspaceConfig
from winter_cli.modules.provision.execution_service import (
    ProvisionExecutionService,
)
from winter_cli.modules.provision.manifest import ProvisionHandler, ProvisionScope
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")
ENV_NAME = "alpha"
ENV_ROOT = WORKSPACE_ROOT / ENV_NAME


# ── Fakes & helpers ───────────────────────────────────────────────────────────


class _InMemoryRegistry:
    """Minimal IEnvIndexRegistry for tests."""

    def __init__(self, assignments: dict[str, int] | None = None) -> None:
        self._data: dict[str, int] = dict(assignments or {})

    def get_index(self, name: str) -> int | None:
        return self._data.get(name)

    def all_assignments(self) -> dict[str, int]:
        return dict(self._data)

    def assign(self, name: str, index: int) -> None:
        self._data[name] = index

    def remove(self, name: str) -> None:
        self._data.pop(name, None)


class _FakeConfigFileReader:
    def __init__(self, contents: dict[Path, dict]) -> None:
        self._contents = contents

    def load(self, path: Path) -> dict:
        if path not in self._contents:
            raise FileNotFoundError(path)
        return self._contents[path]


class FakeProvisionOutputSink:
    """Records every IProvisionOutputSink event for assertion."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, Path]] = []
        self.output_lines: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str, int]] = []
        self.errors: list[tuple[str, str]] = []

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self.started.append((label, action, cwd))

    def execution_output_line(self, label: str, line: str) -> None:
        self.output_lines.append((label, line))

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        self.completed.append((label, action, exit_code))

    def execution_error(self, label: str, error: str) -> None:
        self.errors.append((label, error))


def _make_config(
    *,
    project_repos: list[ProjectRepositoryConfig] | None = None,
    base_port: int = 4000,
    ports_per_env: int = 20,
) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        base_port=base_port,
        ports_per_env=ports_per_env,
        project_repos=project_repos or [],
    )


def _make_service(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict[Path, dict],
    subprocess: FakeSubprocessRunner,
    registry: _InMemoryRegistry | None = None,
) -> ProvisionExecutionService:
    loader = ExtensionManifestLoader(config_file_reader=_FakeConfigFileReader(config_files))
    repo_factory = RepositoryFactory(config=config)
    return ProvisionExecutionService(
        config=config,
        fs=fs,
        subprocess_runner=subprocess,
        manifest_loader=loader,
        repo_factory=repo_factory,
        registry=registry,
    )


def _project_handler(
    subtarget: str = "dependency",
    scope: ProvisionScope = ProvisionScope.workspace,
    apply: tuple[str, ...] = ("echo apply",),
    destroy: tuple[str, ...] | None = None,
    reset: tuple[str, ...] | None = None,
) -> ProvisionHandler:
    return ProvisionHandler(
        subtarget=subtarget,
        scope=scope,
        apply=apply,
        source="project",
        destroy=destroy,
        reset=reset,
    )


def _sh_c_key(command: str) -> str:
    """Return the FakeSubprocessRunner key for sh -c <command>."""
    return f"sh -c {command}"


# ── workspace scope ───────────────────────────────────────────────────────────


def test_workspace_scope_apply_cwd_is_workspace_root() -> None:
    """apply at workspace scope runs with cwd = workspace root."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert len(result.runs) == 1
    assert result.runs[0].cwd == WORKSPACE_ROOT
    assert subprocess.popen_calls[0][1] == WORKSPACE_ROOT


def test_workspace_scope_apply_popen_called_with_sh_c() -> None:
    """apply invokes popen with ['sh', '-c', command]."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert subprocess.popen_calls[0][0] == ["sh", "-c", cmd]


def test_workspace_scope_apply_base_env_no_env_trio() -> None:
    """workspace scope: env contains WINTER_WORKSPACE_DIR but NOT WINTER_ENV*."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    env = subprocess.popen_envs[0]
    assert env["WINTER_WORKSPACE_DIR"] == str(WORKSPACE_ROOT)
    assert "WINTER_ENV" not in env
    assert "WINTER_ENV_INDEX" not in env
    assert "WINTER_PORT_BASE" not in env


def test_workspace_scope_apply_streams_output_to_sink() -> None:
    """Script stdout lines are forwarded to the sink."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): (["line one", "line two"], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    lines = [line for _, line in sink.output_lines]
    assert "line one" in lines
    assert "line two" in lines


# ── feature-environment scope ─────────────────────────────────────────────────


def test_feature_environment_scope_cwd_is_env_root() -> None:
    """apply at feature-environment scope: cwd = <workspace>/<env>."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_environment, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert result.runs[0].cwd == ENV_ROOT
    assert subprocess.popen_calls[0][1] == ENV_ROOT


def test_feature_environment_scope_env_trio_present() -> None:
    """apply at feature-environment scope: WINTER_ENV/WINTER_ENV_INDEX/WINTER_PORT_BASE set."""
    config = _make_config(base_port=4000, ports_per_env=20)
    fs = FakeFilesystem()
    cmd = "echo apply"

    # alpha is alias index 1 → port base 4000 + 1*20 = 4020
    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_environment, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    env = subprocess.popen_envs[0]
    assert env["WINTER_ENV"] == "alpha"
    assert env["WINTER_ENV_INDEX"] == "1"  # alpha is alias 1
    assert env["WINTER_PORT_BASE"] == "4020"


def test_feature_environment_scope_env_trio_uses_registry_index() -> None:
    """WINTER_ENV_INDEX agrees with the registry-persisted index, not the suggestion."""
    config = _make_config(base_port=5000, ports_per_env=30)
    fs = FakeFilesystem()
    cmd = "echo apply"

    registry = _InMemoryRegistry({"alpha": 7})
    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess, registry=registry)

    handler = _project_handler(scope=ProvisionScope.feature_environment, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    env = subprocess.popen_envs[0]
    assert env["WINTER_ENV_INDEX"] == "7"
    assert env["WINTER_PORT_BASE"] == str(5000 + 7 * 30)


# ── feature-worktree scope ────────────────────────────────────────────────────


def test_feature_worktree_scope_runs_once_per_project_repo() -> None:
    """apply at feature-worktree scope: one invocation per project repo."""
    config = _make_config(
        project_repos=[
            ProjectRepositoryConfig(name="app", url="git@example.com:org/app.git"),
            ProjectRepositoryConfig(name="api", url="git@example.com:org/api.git"),
        ]
    )
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_worktree, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert len(result.runs) == 2

    cwds = {r.cwd for r in result.runs}
    assert cwds == {
        WORKSPACE_ROOT / ENV_NAME / "app",
        WORKSPACE_ROOT / ENV_NAME / "api",
    }


def test_feature_worktree_scope_correct_cwd_per_repo() -> None:
    """Each worktree run uses <workspace>/<env>/<repo.name> as cwd."""
    config = _make_config(
        project_repos=[
            ProjectRepositoryConfig(name="myrepo", url="git@example.com:org/myrepo.git"),
        ]
    )
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_worktree, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.runs[0].cwd == WORKSPACE_ROOT / ENV_NAME / "myrepo"
    assert subprocess.popen_calls[0][1] == WORKSPACE_ROOT / ENV_NAME / "myrepo"


def test_feature_worktree_scope_env_trio_present_for_each_run() -> None:
    """WINTER_ENV is set for each worktree invocation."""
    config = _make_config(
        project_repos=[
            ProjectRepositoryConfig(name="repo-a", url="git@example.com:org/repo-a.git"),
            ProjectRepositoryConfig(name="repo-b", url="git@example.com:org/repo-b.git"),
        ]
    )
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_worktree, apply=(cmd,))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert len(subprocess.popen_envs) == 2
    for env in subprocess.popen_envs:
        assert env["WINTER_ENV"] == ENV_NAME
        assert "WINTER_ENV_INDEX" in env
        assert "WINTER_PORT_BASE" in env


# ── exit-code propagation ─────────────────────────────────────────────────────


def test_non_zero_exit_code_captured_in_result() -> None:
    """A non-zero exit code is recorded in SingleRunResult.exit_code and ok=False."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): (["error output"], 1)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert not result.ok
    assert len(result.runs) == 1
    assert result.runs[0].exit_code == 1
    assert sink.completed[0][2] == 1


def test_non_zero_exit_among_worktrees_propagates() -> None:
    """For feature-worktree scope, a failing run makes ok=False even if others pass."""
    config = _make_config(
        project_repos=[
            ProjectRepositoryConfig(name="app", url="git@example.com:org/app.git"),
            ProjectRepositoryConfig(name="api", url="git@example.com:org/api.git"),
        ]
    )
    fs = FakeFilesystem()
    cmd = "echo apply"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): ([], 1)},  # always exit 1 for simplicity
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.feature_worktree, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert not result.ok


# ── multi-command ordered execution ──────────────────────────────────────────


def test_multi_command_apply_runs_in_order() -> None:
    """Multiple commands in apply tuple run in declaration order."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd1 = "echo step1"
    cmd2 = "echo step2"
    cmd3 = "echo step3"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(cmd1): (["step1"], 0),
            _sh_c_key(cmd2): (["step2"], 0),
            _sh_c_key(cmd3): (["step3"], 0),
        },
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd1, cmd2, cmd3))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert len(result.runs) == 1
    assert result.runs[0].exit_code == 0
    # Three popen calls in order.
    assert subprocess.popen_calls[0][0] == ["sh", "-c", cmd1]
    assert subprocess.popen_calls[1][0] == ["sh", "-c", cmd2]
    assert subprocess.popen_calls[2][0] == ["sh", "-c", cmd3]


def test_multi_command_stops_at_first_failure() -> None:
    """A non-zero exit from any command stops subsequent commands in that cwd."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd1 = "echo step1"
    cmd2 = "echo step2_fail"
    cmd3 = "echo step3_should_not_run"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(cmd1): ([], 0),
            _sh_c_key(cmd2): ([], 1),
            # cmd3 is registered but must not be called.
            _sh_c_key(cmd3): ([], 0),
        },
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd1, cmd2, cmd3))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert not result.ok
    assert result.runs[0].exit_code == 1
    # Only two popen calls: cmd1 then cmd2; cmd3 was skipped.
    assert len(subprocess.popen_calls) == 2
    assert subprocess.popen_calls[1][0] == ["sh", "-c", cmd2]


def test_single_string_same_as_single_element_tuple() -> None:
    """A single-command tuple behaves identically to a single-element list."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd = "echo hello"

    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): (["hello"], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd,))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert len(result.runs) == 1
    assert len(subprocess.popen_calls) == 1
    assert subprocess.popen_calls[0][0] == ["sh", "-c", cmd]


def test_multi_command_all_pass_exit_code_zero() -> None:
    """When all commands pass the cwd result exit_code is 0."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd1 = "echo a"
    cmd2 = "echo b"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(cmd1): ([], 0),
            _sh_c_key(cmd2): ([], 0),
        },
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd1, cmd2))
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    assert result.runs[0].exit_code == 0


def test_execution_started_completed_once_per_cwd_multi_command() -> None:
    """execution_started / execution_completed fire once per cwd, not per command."""
    config = _make_config()
    fs = FakeFilesystem()
    cmd1 = "echo a"
    cmd2 = "echo b"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(cmd1): ([], 0),
            _sh_c_key(cmd2): ([], 0),
        },
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, apply=(cmd1, cmd2))
    svc.run_handler(handler, "apply", ENV_NAME, sink)

    # workspace scope → one cwd → one started, one completed
    assert len(sink.started) == 1
    assert len(sink.completed) == 1


# ── destroy / reset actions ───────────────────────────────────────────────────


def test_destroy_action_runs_destroy_commands() -> None:
    """action='destroy' runs handler.destroy commands, not handler.apply."""
    config = _make_config()
    fs = FakeFilesystem()
    apply_cmd = "echo apply"
    destroy_cmd = "echo destroy"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(apply_cmd): ([], 0),
            _sh_c_key(destroy_cmd): (["destroyed"], 0),
        }
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(
        scope=ProvisionScope.workspace,
        apply=(apply_cmd,),
        destroy=(destroy_cmd,),
    )
    result = svc.run_handler(handler, "destroy", ENV_NAME, sink)

    assert result.ok
    assert subprocess.popen_calls[0][0] == ["sh", "-c", destroy_cmd]


def test_reset_action_runs_reset_commands() -> None:
    """action='reset' runs handler.reset commands, not apply or destroy."""
    config = _make_config()
    fs = FakeFilesystem()
    apply_cmd = "echo apply"
    reset_cmd = "echo reset"

    subprocess = FakeSubprocessRunner(
        popen_responses={
            _sh_c_key(apply_cmd): ([], 0),
            _sh_c_key(reset_cmd): (["reset"], 0),
        }
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(
        scope=ProvisionScope.workspace,
        apply=(apply_cmd,),
        reset=(reset_cmd,),
    )
    result = svc.run_handler(handler, "reset", ENV_NAME, sink)

    assert result.ok
    assert subprocess.popen_calls[0][0] == ["sh", "-c", reset_cmd]


def test_destroy_action_returns_error_when_no_destroy_commands() -> None:
    """When handler.destroy is None, destroy action returns an error (caller violated contract)."""
    config = _make_config()
    fs = FakeFilesystem()

    subprocess = FakeSubprocessRunner()
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = _project_handler(scope=ProvisionScope.workspace, destroy=None)
    result = svc.run_handler(handler, "destroy", ENV_NAME, sink)

    assert not result.ok
    assert result.error is not None
    assert not subprocess.popen_calls


# ── extension-source handlers ─────────────────────────────────────────────────


def _setup_extension(
    fs: FakeFilesystem,
    config_files: dict[Path, dict],
    ext_name: str,
) -> Path:
    """Register an extension repo; returns ext_path."""
    ext_path = WORKSPACE_ROOT / ext_name
    fs.directories.add(ext_path)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files[manifest_path] = {"name": ext_name}
    return ext_path


def test_extension_source_env_vars_set_correctly() -> None:
    """Extension handler: WINTER_EXT_DIR and WINTER_EXT_PREFIX reflect the extension."""
    from winter_cli.config.models import StandaloneRepositoryConfig

    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        standalone_repos=[
            StandaloneRepositoryConfig(name="my-ext", url="git@example.com:org/my-ext.git"),
        ],
    )
    fs = FakeFilesystem()
    config_files: dict[Path, dict] = {}
    _setup_extension(fs, config_files, "my-ext")

    cmd = "echo provision"
    subprocess = FakeSubprocessRunner(
        popen_responses={_sh_c_key(cmd): (["ran"], 0)},
    )
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, config_files, subprocess)

    handler = ProvisionHandler(
        subtarget="dependency",
        scope=ProvisionScope.workspace,
        apply=(cmd,),
        source="my-ext",
    )
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert result.ok
    env = subprocess.popen_envs[0]
    assert env["WINTER_EXT_DIR"] == str(WORKSPACE_ROOT / "my-ext")
    assert env["WINTER_EXT_PREFIX"] == "my-ext"


def test_unknown_extension_source_produces_error() -> None:
    """A handler whose source doesn't match any installed extension returns an error."""
    config = _make_config()
    fs = FakeFilesystem()
    subprocess = FakeSubprocessRunner()
    sink = FakeProvisionOutputSink()
    svc = _make_service(config, fs, {}, subprocess)

    handler = ProvisionHandler(
        subtarget="dependency",
        scope=ProvisionScope.workspace,
        apply=("echo apply",),
        source="no-such-ext",
    )
    result = svc.run_handler(handler, "apply", ENV_NAME, sink)

    assert not result.ok
    assert result.error is not None
    assert not subprocess.popen_calls
