"""Tests for provision teardown integration in DestroyService.

Covers:
- Full teardown ordering: provision teardown (data → resource) runs before
  extension hooks and worktree removal.
- Missing destroy handler: warn + no-op, structural teardown still proceeds.
- --no-provision-teardown (provision_teardown=False): skips provision phase.
- --dry-run with provision teardown: previews plan without executing.
- --dry-run with --no-provision-teardown: plan omits provision phase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import (
    FakeConfigFileReader,
    FakeEnvIndexRegistry,
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
from winter_cli.modules.provision.manifest import ProvisionAction, ProvisionHandler
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

WORKSPACE_ROOT = Path("/ws")
DEMO_MAIN = WORKSPACE_ROOT / "projects" / "demo"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHandlerExecutionResult:
    def __init__(self, handler: ProvisionHandler, action: ProvisionAction, ok: bool = True) -> None:
        self.handler = handler
        self.action = action
        self.runs: tuple[Any, ...] = ()
        self.error: str | None = None if ok else "fake failure"
        self._ok = ok

    @property
    def ok(self) -> bool:
        return self._ok


class _RecordingExecutionService:
    """Records run_handler calls; returns ok by default."""

    def __init__(self) -> None:
        self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

    def run_handler(
        self,
        handler: ProvisionHandler,
        action: ProvisionAction,
        env_name: str,
        sink: Any,
    ) -> _FakeHandlerExecutionResult:
        self.calls.append((handler, action, env_name))
        return _FakeHandlerExecutionResult(handler=handler, action=action, ok=True)


class _FakeManifestLoader:
    def __init__(self) -> None:
        pass

    def load(self, repo: Any, manifest_path: Path) -> Any:
        raise ValueError(f"no manifest registered for {manifest_path}")


class _FakeRepoFactory:
    def get_standalone_repos(self) -> list[Any]:
        return []

    def get_extension_repos(self) -> list[Any]:
        return []

    def get_project_repos(self) -> list[Any]:
        return []


class _FakeProvisionReporter:
    """Records IProvisionReporter events for assertion."""

    def __init__(self) -> None:
        self.provision_started_calls: list[tuple[str, list[str], ProvisionAction]] = []
        self.subtarget_started_calls: list[str] = []
        self.no_handlers_calls: list[str] = []
        self.handler_result_calls: list[dict[str, Any]] = []
        self.handler_warn_calls: list[dict[str, Any]] = []
        self.plan_handler_calls: list[dict[str, Any]] = []
        self.provision_finished_calls: list[tuple[str, str | None]] = []

    def provision_started(self, env: str, subtargets: list[str], action: ProvisionAction) -> None:
        self.provision_started_calls.append((env, subtargets, action))

    def subtarget_started(self, subtarget: str) -> None:
        self.subtarget_started_calls.append(subtarget)

    def no_handlers(self, subtarget: str) -> None:
        self.no_handlers_calls.append(subtarget)

    def handler_result(
        self,
        subtarget: str,
        scope: str,
        source: str,
        action: str,
        service_check: str | None,
        runs: list[dict[str, Any]],
        exit_status: int,
    ) -> None:
        self.handler_result_calls.append(
            {
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "action": action,
                "exit_status": exit_status,
            }
        )

    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None:
        self.handler_warn_calls.append(
            {
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "message": message,
            }
        )

    def plan_handler(
        self,
        subtarget: str,
        scope: str,
        source: str,
        commands: list[str],
        action: str,
        required_services: list[str],
        service_check_preview: str | None,
        cwd: str,
        project: str | None = None,
    ) -> None:
        self.plan_handler_calls.append(
            {
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "commands": commands,
                "action": action,
                "cwd": cwd,
                "project": project,
            }
        )

    def provision_finished(self, status: str, aborted_at: str | None) -> None:
        self.provision_finished_calls.append((status, aborted_at))

    # IProvisionOutputSink stubs
    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        pass

    def execution_output_line(self, label: str, line: str) -> None:
        pass

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        pass

    def execution_error(self, label: str, error: str) -> None:
        pass


class _FakeProvisionService:
    """Records run() calls to verify ordering; delegates to a real ProvisionService for dry-run tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        env_name: str,
        subtarget: str | None,
        action: ProvisionAction,
        seed: bool,
        no_service_check: bool,
        reporter: Any,
        dry_run: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "env_name": env_name,
                "subtarget": subtarget,
                "destroy": action is ProvisionAction.destroy,
                "dry_run": dry_run,
            }
        )
        # Mirror the real ProvisionService lifecycle events so reporter adapters
        # behave correctly (e.g. _DestroyProvisionReporter._ensure_started fires).
        reporter.provision_started(env_name, [subtarget] if subtarget else [], action.value)
        reporter.provision_finished(status="ok", aborted_at=None)

        class _OkSummary:
            status = "ok"
            exit_code = 0

        return _OkSummary()


class _FailingProvisionService:
    """Fake ProvisionService that returns a failed summary for a named subtarget."""

    def __init__(self, fail_subtarget: str = "data") -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_subtarget = fail_subtarget

    def run(
        self,
        env_name: str,
        subtarget: str | None,
        action: ProvisionAction,
        seed: bool,
        no_service_check: bool,
        reporter: Any,
        dry_run: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "env_name": env_name,
                "subtarget": subtarget,
                "destroy": action is ProvisionAction.destroy,
                "dry_run": dry_run,
            }
        )
        # Emit lifecycle events so the reporter adapter behaves correctly.
        reporter.provision_started(env_name, [subtarget] if subtarget else [], action.value)

        if subtarget == self.fail_subtarget:
            reporter.provision_finished(status="error", aborted_at=None)

            class _ErrorSummary:
                status = "error"
                exit_code = 1

            return _ErrorSummary()

        reporter.provision_finished(status="ok", aborted_at=None)

        class _OkSummary:
            status = "ok"
            exit_code = 0

        return _OkSummary()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace_config(provision_raw: dict | None = None) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        project_repos=[
            ProjectRepositoryConfig(name="demo", url="git@example.com:org/demo.git"),
        ],
        provision_raw=provision_raw or {},
    )


def _service(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    git: FakeGitRepository,
    provision_svc: Any | None = None,
    registry: FakeEnvIndexRegistry | None = None,
) -> DestroyService:
    hook_svc = ExtensionHookService(
        config=config,
        fs=fs,
        subprocess_runner=FakeSubprocessRunner(),
        manifest_loader=ExtensionManifestLoader(config_file_reader=FakeConfigFileReader({})),
    )
    return DestroyService(
        config=config,
        repo_factory=RepositoryFactory(config),
        extension_hook_svc=hook_svc,
        fs=fs,
        git_repo=git,
        registry=registry or FakeEnvIndexRegistry(),
        provision_svc=provision_svc,
    )


def _minimal_fs(env_name: str = "alpha") -> FakeFilesystem:
    env_root = WORKSPACE_ROOT / env_name
    worktree_path = env_root / "demo"
    return FakeFilesystem(
        directories=[WORKSPACE_ROOT / "projects", DEMO_MAIN, env_root, worktree_path],
        files={
            WORKSPACE_ROOT / ".git" / "info" / "exclude": "",
        },
    )


# ---------------------------------------------------------------------------
# Ordering tests
# ---------------------------------------------------------------------------


def test_provision_teardown_runs_data_then_resource_before_removal(
    init_reporter: FakeInitReporter,
) -> None:
    """Provision teardown runs data --destroy, resource --destroy, then structural removal."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    assert ok is True
    # Two calls: data first, then resource (reverse of apply order).
    assert len(provision_svc.calls) == 2
    assert provision_svc.calls[0]["subtarget"] == "data"
    assert provision_svc.calls[0]["destroy"] is True
    assert provision_svc.calls[1]["subtarget"] == "resource"
    assert provision_svc.calls[1]["destroy"] is True
    # Env was also removed (structural teardown ran).
    assert not fs.exists(WORKSPACE_ROOT / "alpha")


def test_provision_teardown_uses_correct_env_name(
    init_reporter: FakeInitReporter,
) -> None:
    """Provision teardown is called with the env name matching the destroy target."""
    config = _workspace_config()
    fs = _minimal_fs("beta")
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "beta" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    svc.destroy_env(
        "beta",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    for call in provision_svc.calls:
        assert call["env_name"] == "beta"


# ---------------------------------------------------------------------------
# Missing destroy handler — warn + no-op
# ---------------------------------------------------------------------------


def test_provision_teardown_missing_handler_warns_and_continues(
    init_reporter: FakeInitReporter,
) -> None:
    """When no destroy script is declared, provision service warns + no-ops; structural teardown still runs."""
    from winter_cli.modules.provision.provision_service import NoOpServiceCheck, ProvisionService

    config = _workspace_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            # No destroy script → should warn and no-op.
        }
    )
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    exec_svc = _RecordingExecutionService()
    prov_svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),  # type: ignore[arg-type]
        repo_factory=_FakeRepoFactory(),  # type: ignore[arg-type]
        service_check=NoOpServiceCheck(),
        fs=FakeFilesystem(directories=[WORKSPACE_ROOT / "alpha"]),
    )

    svc = _service(config, fs, git, provision_svc=prov_svc)
    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    assert ok is True
    # No handler scripts were executed (no destroy declared).
    assert len(exec_svc.calls) == 0
    # Structural teardown still ran — env removed.
    assert not fs.exists(WORKSPACE_ROOT / "alpha")
    # Reporter received warn actions from the provision reporter adapter.
    action_kinds = [a[2] for a in init_reporter.actions]
    assert "provision_handler_warn" in action_kinds


# ---------------------------------------------------------------------------
# --no-provision-teardown skip flag
# ---------------------------------------------------------------------------


def test_no_provision_teardown_skips_provision_phase(
    init_reporter: FakeInitReporter,
) -> None:
    """provision_teardown=False skips provision teardown; structural teardown still runs."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=False,
    )

    assert ok is True
    # No provision calls at all.
    assert len(provision_svc.calls) == 0
    # Structural teardown still ran.
    assert not fs.exists(WORKSPACE_ROOT / "alpha")


def test_no_provision_teardown_also_skips_when_provision_svc_is_none(
    init_reporter: FakeInitReporter,
) -> None:
    """When provision_svc is None, destroy_env proceeds without error."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    svc = _service(config, fs, git, provision_svc=None)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    # With no provision_svc, teardown still completes successfully.
    assert ok is True
    assert not fs.exists(WORKSPACE_ROOT / "alpha")


# ---------------------------------------------------------------------------
# Dry-run with provision teardown
# ---------------------------------------------------------------------------


def test_dry_run_emits_provision_teardown_plan_then_structural_plan(
    init_reporter: FakeInitReporter,
) -> None:
    """dry_run=True previews provision teardown (data, resource) then structural plan."""
    config = _workspace_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "destroy": "scripts/drop.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh", "destroy": "scripts/unseed.sh"}],
        }
    )
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=True,
        reporter=init_reporter,
        provision_teardown=True,
    )

    assert ok is True
    # provision plan was requested: two calls (data, resource).
    assert len(provision_svc.calls) == 2
    assert provision_svc.calls[0]["subtarget"] == "data"
    assert provision_svc.calls[0]["dry_run"] is True
    assert provision_svc.calls[1]["subtarget"] == "resource"
    assert provision_svc.calls[1]["dry_run"] is True
    # Structural plan events also emitted.
    action_kinds = [a[2] for a in init_reporter.actions]
    assert "would_remove_worktree" in action_kinds
    assert "would_remove_env" in action_kinds
    # Nothing was actually removed.
    assert fs.exists(WORKSPACE_ROOT / "alpha")


def test_dry_run_no_provision_teardown_omits_provision_phase(
    init_reporter: FakeInitReporter,
) -> None:
    """dry_run=True with provision_teardown=False omits provision plan."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=True,
        reporter=init_reporter,
        provision_teardown=False,
    )

    assert ok is True
    # No provision calls at all.
    assert len(provision_svc.calls) == 0
    # Structural dry-run plan still emitted.
    action_kinds = [a[2] for a in init_reporter.actions]
    assert "would_remove_worktree" in action_kinds
    assert "would_remove_env" in action_kinds
    # Nothing removed.
    assert fs.exists(WORKSPACE_ROOT / "alpha")


# ---------------------------------------------------------------------------
# CLI flag wiring
# ---------------------------------------------------------------------------


def test_ws_destroy_help_includes_no_provision_teardown_flag() -> None:
    """--no-provision-teardown appears in ws destroy --help."""
    from click.testing import CliRunner

    from winter_cli.modules.workspace.command import ws_destroy

    runner = CliRunner()
    result = runner.invoke(ws_destroy, ["--help"])
    assert result.exit_code == 0
    assert "--no-provision-teardown" in result.output


# ---------------------------------------------------------------------------
# Failing teardown — strict vs non-strict behaviour (Finding 1)
# ---------------------------------------------------------------------------


def test_failing_teardown_strict_aborts_before_structural_removal(
    init_reporter: FakeInitReporter,
) -> None:
    """--strict: a failing provision teardown aborts before worktree/env removal."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FailingProvisionService(fail_subtarget="data")
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=True,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    # Should fail and abort.
    assert ok is False
    # Structural removal must NOT have run — env dir still present.
    assert fs.exists(WORKSPACE_ROOT / "alpha")
    # Reporter received an error.
    error_messages = [msg for _, msg in init_reporter.errors]
    assert any("provision teardown" in msg for msg in error_messages)


def test_failing_teardown_non_strict_surfaces_error_and_proceeds(
    init_reporter: FakeInitReporter,
) -> None:
    """Non-strict: a failing provision teardown surfaces error but structural teardown runs."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FailingProvisionService(fail_subtarget="data")
    svc = _service(config, fs, git, provision_svc=provision_svc)

    ok = svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    # Returns False because teardown failed.
    assert ok is False
    # But structural removal still ran — env dir gone.
    assert not fs.exists(WORKSPACE_ROOT / "alpha")
    # Reporter received an error (the failure is surfaced).
    error_messages = [msg for _, msg in init_reporter.errors]
    assert any("provision teardown" in msg for msg in error_messages)


def test_teardown_finished_event_emitted_once_on_failure(
    init_reporter: FakeInitReporter,
) -> None:
    """provision_teardown_finished is emitted exactly once even on teardown failure."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FailingProvisionService(fail_subtarget="data")
    svc = _service(config, fs, git, provision_svc=provision_svc)

    svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    finished_events = [a for a in init_reporter.actions if a[2] == "provision_teardown_finished"]
    assert len(finished_events) == 1
    # Status should reflect the failure.
    assert finished_events[0][3] == "error"


def test_teardown_started_event_emitted_once(
    init_reporter: FakeInitReporter,
) -> None:
    """provision_teardown_started is emitted exactly once for the two-phase teardown."""
    config = _workspace_config()
    fs = _minimal_fs()
    git = FakeGitRepository()
    git.clean_worktrees.add(WORKSPACE_ROOT / "alpha" / "demo")

    provision_svc = _FakeProvisionService()
    svc = _service(config, fs, git, provision_svc=provision_svc)

    svc.destroy_env(
        "alpha",
        force=False,
        strict=False,
        dry_run=False,
        reporter=init_reporter,
        provision_teardown=True,
    )

    started_events = [a for a in init_reporter.actions if a[2] == "provision_teardown_started"]
    assert len(started_events) == 1
