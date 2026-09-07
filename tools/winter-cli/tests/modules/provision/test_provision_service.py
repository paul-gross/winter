"""Tests for ProvisionService — ordering, abort semantics, action vocabulary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.provision.manifest import ProvisionAction, ProvisionHandler, ProvisionScope
from winter_cli.modules.provision.provision_service import NoOpServiceCheck, ProvisionService

WORKSPACE_ROOT = Path("/ws")
ENV_NAME = "alpha"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHandlerExecutionResult:
    """Minimal HandlerExecutionResult-alike returned by the fake execution service."""

    def __init__(self, handler: ProvisionHandler, action: ProvisionAction, ok: bool) -> None:
        self.handler = handler
        self.action = action
        self.runs: tuple[Any, ...] = ()
        self.error: str | None = None if ok else "fake failure"
        self._ok = ok

    @property
    def ok(self) -> bool:
        return self._ok


class _FakeExecutionService:
    """Records run_handler calls in order; returns canned ok/fail by (handler_id, action)."""

    def __init__(self, fail_on: set[tuple[int, ProvisionAction]] | None = None) -> None:
        # fail_on: set of (id(handler), action) pairs that should return ok=False
        self._fail_on: set[tuple[int, ProvisionAction]] = fail_on or set()
        self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

    def run_handler(
        self,
        handler: ProvisionHandler,
        action: ProvisionAction,
        env_name: str,
        sink: Any,
    ) -> _FakeHandlerExecutionResult:
        self.calls.append((handler, action, env_name))
        ok = (id(handler), action) not in self._fail_on
        return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)


class _FakeManifestLoader:
    """Returns canned ExtensionManifest per path."""

    def __init__(self, manifests: dict[Path, Any] | None = None) -> None:
        self._manifests = manifests or {}

    def load(self, repo: Any, manifest_path: Path | None) -> Any:
        if manifest_path in self._manifests:
            return self._manifests[manifest_path]
        raise ValueError(f"no manifest registered for {manifest_path}")


class _FakeRepoFactory:
    """Returns a configured list of standalone repos."""

    def __init__(self, standalone: list[Any] | None = None, project: list[Any] | None = None) -> None:
        self._standalone: list[Any] = standalone or []
        self._project: list[Any] = project or []

    def get_standalone_repos(self) -> list[Any]:
        return list(self._standalone)

    def get_extension_repos(self) -> list[Any]:
        return list(self._standalone)

    def get_project_repos(self) -> list[Any]:
        return list(self._project)


class _FakeReporter:
    """Records every IProvisionReporter event."""

    def __init__(self) -> None:
        self.provision_started_calls: list[tuple[str, list[str], ProvisionAction]] = []
        self.subtarget_started_calls: list[str] = []
        self.no_handlers_calls: list[str] = []
        self.handler_result_calls: list[dict[str, Any]] = []
        self.handler_warn_calls: list[dict[str, Any]] = []
        self.plan_handler_calls: list[dict[str, Any]] = []
        self.provision_finished_calls: list[tuple[str, str | None]] = []
        # Execution sink events
        self.execution_started_calls: list[Any] = []
        self.execution_output_lines: list[Any] = []
        self.execution_completed_calls: list[Any] = []
        self.execution_errors: list[Any] = []

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
                "service_check": service_check,
                "exit_status": exit_status,
            }
        )

    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None:
        self.handler_warn_calls.append({"subtarget": subtarget, "scope": scope, "source": source, "message": message})

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
                "required_services": required_services,
                "service_check_preview": service_check_preview,
                "cwd": cwd,
                "project": project,
            }
        )

    def provision_finished(self, status: str, aborted_at: str | None) -> None:
        self.provision_finished_calls.append((status, aborted_at))

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self.execution_started_calls.append((label, action, cwd))

    def execution_output_line(self, label: str, line: str) -> None:
        self.execution_output_lines.append((label, line))

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        self.execution_completed_calls.append((label, action, exit_code))

    def execution_error(self, label: str, error: str) -> None:
        self.execution_errors.append((label, error))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(provision_raw: dict | None = None) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        provision_raw=provision_raw or {},
    )


def _make_handler(
    subtarget: str,
    scope: ProvisionScope,
    source: str = "project",
    apply: tuple[str, ...] = ("scripts/apply.sh",),
    destroy: tuple[str, ...] | None = None,
    reset: tuple[str, ...] | None = None,
) -> ProvisionHandler:
    return ProvisionHandler(
        subtarget=subtarget,
        scope=scope,
        apply=apply,
        source=source,
        destroy=destroy,
        reset=reset,
    )


def _make_fs_with_env(env_name: str = ENV_NAME) -> FakeFilesystem:
    """Return a FakeFilesystem that has the env directory present."""
    return FakeFilesystem(directories=[WORKSPACE_ROOT / env_name])


def _make_service(
    *,
    config: WorkspaceConfig | None = None,
    execution_svc: _FakeExecutionService | None = None,
    repo_factory: _FakeRepoFactory | None = None,
    fs: FakeFilesystem | None = None,
    env_name: str = ENV_NAME,
) -> tuple[ProvisionService, _FakeExecutionService, _FakeReporter]:
    cfg = config or _make_config()
    exec_svc = execution_svc or _FakeExecutionService()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=cfg,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=repo_factory or _FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=fs or _make_fs_with_env(env_name),
    )
    return svc, exec_svc, reporter


# ---------------------------------------------------------------------------
# Chain ordering: dependency → resource → data
# ---------------------------------------------------------------------------


def test_full_chain_runs_subtargets_in_order() -> None:
    """Full chain runs dependency → resource → data in that exact order."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    # Reporter sees subtarget_started in order
    assert reporter.subtarget_started_calls == ["dependency", "resource", "data"]
    # All three exec calls happened
    assert len(exec_svc.calls) == 3
    subtargets_run = [call[0].subtarget for call in exec_svc.calls]
    assert subtargets_run == ["dependency", "resource", "data"]


def test_scope_substrate_first_within_subtarget() -> None:
    """Within a sub-target, workspace scope runs before feature-environment, before feature-worktree."""
    # Declare in reverse order in the config to verify sort overrides declaration order.
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "feature-worktree", "apply": "scripts/apply.sh"},
                {"scope": "feature-environment", "apply": "scripts/apply.sh"},
                {"scope": "workspace", "apply": "scripts/apply.sh"},
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    scopes = [call[0].scope for call in exec_svc.calls]
    assert scopes == [ProvisionScope.workspace, ProvisionScope.feature_environment, ProvisionScope.feature_worktree]


def test_project_before_extension_within_same_scope() -> None:
    """Project source runs before extension source at the same scope."""
    from winter_cli.modules.workspace.models import StandaloneRepository

    class _FakeExtManifest:
        provision = (
            ProvisionHandler(
                subtarget="dependency",
                scope=ProvisionScope.workspace,
                apply=("scripts/apply.sh",),
                source="my-ext",
            ),
        )

    fake_repo = StandaloneRepository(name="my-ext", path=WORKSPACE_ROOT / "my-ext")
    manifest_path = WORKSPACE_ROOT / "my-ext" / "winter-ext.toml"

    fs = FakeFilesystem(files={manifest_path: ""}, directories=[WORKSPACE_ROOT / ENV_NAME])
    loader = _FakeManifestLoader(manifests={manifest_path: _FakeExtManifest()})
    repo_factory = _FakeRepoFactory(standalone=[fake_repo])

    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    exec_svc = _FakeExecutionService()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=loader,  # type: ignore[arg-type]
        repo_factory=repo_factory,  # type: ignore[arg-type]
        service_check=NoOpServiceCheck(),
        fs=fs,
    )
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    sources = [call[0].source for call in exec_svc.calls]
    assert sources[0] == "project"
    assert sources[1] == "my-ext"


def test_collects_handlers_from_extension_repo_path_not_workspace_root_name() -> None:
    """_collect_all_handlers reads the manifest from repo.path — the actual on-disk
    checkout — rather than assuming workspace_root/repo.name. Regression for a
    project-repo extension (e.g. projects/winter-docs), whose path never equals
    workspace_root/<name>, and for any standalone installed at a custom `path`."""
    from winter_cli.modules.workspace.models import StandaloneRepository

    class _FakeExtManifest:
        provision = (
            ProvisionHandler(
                subtarget="dependency",
                scope=ProvisionScope.workspace,
                apply=("scripts/apply.sh",),
                source="winter-docs",
            ),
        )

    custom_path = WORKSPACE_ROOT / "projects" / "winter-docs"
    fake_repo = StandaloneRepository(name="winter-docs", path=custom_path)
    manifest_path = custom_path / "winter-ext.toml"

    fs = FakeFilesystem(files={manifest_path: ""}, directories=[WORKSPACE_ROOT / ENV_NAME])
    loader = _FakeManifestLoader(manifests={manifest_path: _FakeExtManifest()})
    repo_factory = _FakeRepoFactory(standalone=[fake_repo])

    config = _make_config(provision_raw={})
    exec_svc = _FakeExecutionService()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=loader,  # type: ignore[arg-type]
        repo_factory=repo_factory,  # type: ignore[arg-type]
        service_check=NoOpServiceCheck(),
        fs=fs,
    )
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    sources = [call[0].source for call in exec_svc.calls]
    assert sources == ["winter-docs"]


def test_declaration_order_tiebreak() -> None:
    """Within same scope and same source, declaration order is preserved."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "workspace", "apply": "scripts/first.sh"},
                {"scope": "workspace", "apply": "scripts/second.sh"},
                {"scope": "workspace", "apply": "scripts/third.sh"},
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    apply_scripts = [call[0].apply for call in exec_svc.calls]
    assert apply_scripts == [
        ("scripts/first.sh",),
        ("scripts/second.sh",),
        ("scripts/third.sh",),
    ]


# ---------------------------------------------------------------------------
# Abort semantics
# ---------------------------------------------------------------------------


def test_apply_failure_in_dependency_aborts_resource_and_data() -> None:
    """A failing apply in dependency aborts resource and data (remaining sub-targets)."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    _svc, _exec_svc, reporter = _make_service(config=config)

    # Mark the dependency handler's apply as failing.
    # We need to identify it after collection.  Inject via a custom exec_svc
    # that fails on the first call.
    class _FailFirstCallExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            # First call (dependency apply) fails
            ok = len(self.calls) != 1
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailFirstCallExecSvc()
    reporter = _FakeReporter()
    svc2 = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc2.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "aborted"
    assert summary.aborted_at == "dependency"
    assert summary.exit_code == 1

    # Only dependency was executed (1 call)
    assert len(fail_exec.calls) == 1
    assert fail_exec.calls[0][0].subtarget == "dependency"

    # Reporter shows aborted_at=dependency
    assert reporter.provision_finished_calls == [("aborted", "dependency")]

    # subtarget_started only called for dependency (resource and data never started)
    assert reporter.subtarget_started_calls == ["dependency"]


def test_apply_failure_stops_within_subtarget_too() -> None:
    """A failing apply in the first handler of a sub-target stops subsequent handlers."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "workspace", "apply": "scripts/first.sh"},
                {"scope": "workspace", "apply": "scripts/second.sh"},
            ],
        }
    )

    class _FailFirstCallExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            ok = len(self.calls) != 1
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailFirstCallExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    # Second handler never ran
    assert len(fail_exec.calls) == 1
    assert summary.status == "aborted"


# ---------------------------------------------------------------------------
# No-handlers no-op
# ---------------------------------------------------------------------------


def test_no_handlers_emits_no_handlers_event_and_finishes_ok() -> None:
    """Empty manifest → three no_handlers events, finished ok."""
    config = _make_config(provision_raw={})
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert summary.exit_code == 0
    assert reporter.no_handlers_calls == ["dependency", "resource", "data"]
    assert len(exec_svc.calls) == 0
    assert reporter.provision_finished_calls == [("ok", None)]


def test_single_subtarget_no_handlers_emits_one_no_handlers_event() -> None:
    """Explicit single sub-target with no handlers → one no_handlers event, ok."""
    config = _make_config(provision_raw={})
    svc, _exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert reporter.no_handlers_calls == ["dependency"]


# ---------------------------------------------------------------------------
# --destroy action
# ---------------------------------------------------------------------------


def test_destroy_flag_runs_destroy_script_when_declared() -> None:
    """--destroy: handler with destroy script → runs destroy action."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "destroy": "scripts/destroy.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][1] is ProvisionAction.destroy


def test_destroy_flag_warns_and_noop_when_no_destroy_declared() -> None:
    """--destroy: handler without destroy script → warn event, no exec call, no error."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 0
    assert len(reporter.handler_warn_calls) == 1
    assert "skipping" in reporter.handler_warn_calls[0]["message"]


# ---------------------------------------------------------------------------
# --reset action
# ---------------------------------------------------------------------------


def test_reset_uses_declared_reset_script() -> None:
    """--reset: handler with reset script → runs reset action."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "workspace", "apply": "scripts/apply.sh", "reset": "scripts/reset.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME, subtarget="data", action=ProvisionAction.reset, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][1] is ProvisionAction.reset


def test_reset_composes_destroy_then_apply_when_no_reset_but_destroy_declared() -> None:
    """--reset: no reset script but destroy declared → destroy then apply."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "destroy": "scripts/destroy.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 2
    assert exec_svc.calls[0][1] is ProvisionAction.destroy
    assert exec_svc.calls[1][1] is ProvisionAction.apply


def test_reset_warns_and_degrades_to_apply_when_neither_reset_nor_destroy() -> None:
    """--reset: no reset and no destroy → warn, run apply."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME, subtarget="data", action=ProvisionAction.reset, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][1] is ProvisionAction.apply
    assert len(reporter.handler_warn_calls) == 1
    assert "degrading" in reporter.handler_warn_calls[0]["message"]


# ---------------------------------------------------------------------------
# clean action
# ---------------------------------------------------------------------------


def test_clean_runs_declared_clean_script() -> None:
    """clean: handler with a declared clean script → runs the clean action."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][1] is ProvisionAction.clean
    assert len(reporter.handler_warn_calls) == 0


def test_clean_filters_out_handler_with_no_clean_declared() -> None:
    """clean: a handler declaring no clean contributes nothing; its sibling still runs."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "workspace", "apply": "scripts/apply-a.sh", "clean": "scripts/clean-a.sh"},
                {"scope": "workspace", "apply": "scripts/apply-b.sh"},
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].apply == ("scripts/apply-a.sh",)
    # The handler with no declared clean is silently absent — not a warning or error.
    assert len(reporter.handler_warn_calls) == 0


def test_clean_subtarget_with_nothing_declared_reports_no_handlers_and_skips_service_check() -> None:
    """clean: a sub-target where nothing declares clean is a no-op — no_handlers, no service check."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "required_services": ["workspace/db"]}],
        }
    )
    sc = _RecordingServiceCheck(result="ok")
    svc, exec_svc, reporter = _make_service_with_check(config, sc)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert reporter.no_handlers_calls == ["resource"]
    assert len(exec_svc.calls) == 0
    assert len(sc.ensure_calls) == 0


def test_clean_full_chain_runs_in_dependency_resource_data_order() -> None:
    """clean with no explicit --stage fans out across the full chain in declared order."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh", "clean": "scripts/clean-dep.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "clean": "scripts/clean-res.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/dat.sh", "clean": "scripts/clean-dat.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.clean, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert reporter.subtarget_started_calls == ["dependency", "resource", "data"]
    assert [h.subtarget for h, _action, _env in exec_svc.calls] == ["dependency", "resource", "data"]
    assert all(action is ProvisionAction.clean for _h, action, _env in exec_svc.calls)


def test_clean_name_selector_without_clean_warns() -> None:
    """--name selecting a handler with no declared clean warns and no-ops (destroy's treatment)."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 0
    assert len(reporter.handler_warn_calls) == 1
    assert "skipping" in reporter.handler_warn_calls[0]["message"]


def test_clean_name_selector_without_clean_starts_no_service() -> None:
    """--name selecting a handler with required_services but no declared clean must not
    start those services — _run_clean warns and no-ops, so the handler has no work for
    the service check to protect."""
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/create-db.sh",
                    "required_services": ["workspace/db"],
                    "name": "mydb",
                }
            ],
        }
    )
    sc = _RecordingServiceCheck(result="ok")
    svc, exec_svc, reporter = _make_service_with_check(config, sc)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 0
    assert len(reporter.handler_warn_calls) == 1
    assert "skipping" in reporter.handler_warn_calls[0]["message"]
    # The service check sees no handlers with actual work — it starts nothing.
    assert sc.ensure_calls == [([], ENV_NAME, False)]


def test_clean_name_selector_with_clean_runs_only_the_named_entry() -> None:
    """--name selecting a handler that declares clean runs only that entry."""
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/create-db.sh",
                    "clean": "scripts/clean-db.sh",
                    "name": "mydb",
                },
                {
                    "scope": "workspace",
                    "apply": "scripts/create-bucket.sh",
                    "clean": "scripts/clean-bucket.sh",
                    "name": "mybucket",
                },
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].name == "mydb"
    assert exec_svc.calls[0][1] is ProvisionAction.clean


def test_failing_clean_produces_error_summary() -> None:
    """A failing clean script → summary.status='error', exit_code=1, no chain-abort label."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )

    class _FailCleanExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=False)

    fail_exec = _FailCleanExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "error"
    assert summary.exit_code == 1
    assert reporter.provision_finished_calls == [("error", None)]
    assert len(fail_exec.calls) == 1
    assert fail_exec.calls[0][1] is ProvisionAction.clean


def test_failing_clean_in_dependency_does_not_abort_resource_and_data() -> None:
    """A failing clean in dependency still runs resource and data — best-effort, not chain-abort.

    Mirrors `test_apply_failure_in_dependency_aborts_resource_and_data`, but for
    `clean` the opposite must hold: every sub-target still runs, and every
    failure is reported, while the overall run still ends non-ok.
    """
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh", "clean": "scripts/clean-dep.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "clean": "scripts/clean-res.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/dat.sh", "clean": "scripts/clean-dat.sh"}],
        }
    )

    class _FailFirstCallExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            # Only the first call (dependency's clean) fails.
            ok = len(self.calls) != 1
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailFirstCallExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.clean, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "error"
    assert summary.exit_code == 1

    # All three sub-targets ran despite dependency's clean failing.
    assert reporter.subtarget_started_calls == ["dependency", "resource", "data"]
    assert [h.subtarget for h, _action, _env in fail_exec.calls] == ["dependency", "resource", "data"]

    # Every failure is reported, not just the first: dependency's handler_result
    # shows the failing exit status alongside resource/data's successes.
    assert [r["exit_status"] for r in reporter.handler_result_calls] == [1, 0, 0]
    assert reporter.provision_finished_calls == [("error", None)]


def test_failing_clean_does_not_stop_sibling_handlers_in_same_subtarget() -> None:
    """A failing clean handler still lets its siblings in the same sub-target run."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "workspace", "apply": "scripts/apply-a.sh", "clean": "scripts/clean-a.sh"},
                {"scope": "workspace", "apply": "scripts/apply-b.sh", "clean": "scripts/clean-b.sh"},
            ],
        }
    )

    class _FailFirstCallExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            ok = len(self.calls) != 1
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailFirstCallExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "error"
    assert len(fail_exec.calls) == 2
    assert [r["exit_status"] for r in reporter.handler_result_calls] == [1, 0]


# ---------------------------------------------------------------------------
# --seed action
# ---------------------------------------------------------------------------


def test_seed_runs_resource_apply_then_data_apply() -> None:
    """--seed on resource: runs resource apply → data apply."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=True,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert reporter.subtarget_started_calls == ["resource", "data"]
    assert len(exec_svc.calls) == 2
    assert exec_svc.calls[0][0].subtarget == "resource"
    assert exec_svc.calls[0][1] is ProvisionAction.apply
    assert exec_svc.calls[1][0].subtarget == "data"
    assert exec_svc.calls[1][1] is ProvisionAction.apply


# ---------------------------------------------------------------------------
# Explicit single sub-target
# ---------------------------------------------------------------------------


def test_explicit_subtarget_runs_only_that_one() -> None:
    """Explicit sub-target only runs that one, not the full chain."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert reporter.subtarget_started_calls == ["resource"]
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].subtarget == "resource"


# ---------------------------------------------------------------------------
# Command-level validation via CliRunner
# ---------------------------------------------------------------------------


def test_provision_help_is_registered() -> None:
    """'winter provision --help' exits 0 and lists the command."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["--help"])
    assert result.exit_code == 0
    assert "provision" in result.output.lower() or "env" in result.output.lower()


def test_provision_rejects_reset_and_destroy_together() -> None:
    """--reset and --destroy together raise ClickException."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--stage", "resource", "--reset", "--destroy"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_provision_rejects_seed_without_resource_subtarget() -> None:
    """--seed without explicit 'resource' sub-target raises ClickException."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--stage", "data", "--seed"])
    assert result.exit_code != 0
    assert "resource" in result.output.lower()


def test_provision_rejects_reset_without_subtarget() -> None:
    """--reset without an explicit sub-target raises ClickException."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--reset"])
    assert result.exit_code != 0


def test_provision_rejects_destroy_without_subtarget() -> None:
    """--destroy without an explicit sub-target raises ClickException."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--destroy"])
    assert result.exit_code != 0


def test_provision_accepts_reset_with_name_and_no_stage() -> None:
    """--reset with --name (no --stage) is accepted at the command-validation level."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    # No container is wired in this bare CliRunner invocation, so this will fail
    # further down the stack — but NOT at the flag-combination validation step.
    result = runner.invoke(provision_command, ["alpha", "--name", "workspace.mydb", "--reset"])
    assert "require an explicit --stage or --name" not in (result.output or "")


def test_provision_accepts_destroy_with_name_and_no_stage() -> None:
    """--destroy with --name (no --stage) is accepted at the command-validation level."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--name", "workspace.mydb", "--destroy"])
    assert "require an explicit --stage or --name" not in (result.output or "")


def test_provision_rejects_seed_combined_with_name() -> None:
    """--seed cannot be combined with --name."""
    from click.testing import CliRunner

    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--stage", "resource", "--seed", "--name", "workspace.mydb"])
    assert result.exit_code != 0
    assert "--name" in result.output


# ---------------------------------------------------------------------------
# Service-check integration: ensure() called before handlers; raised error aborts
# ---------------------------------------------------------------------------


class _RecordingServiceCheck:
    """Records ensure() calls and delegates to a canned outcome."""

    def __init__(self, result: str | None = "ok", raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.ensure_calls: list[tuple[list[ProvisionHandler], str, bool]] = []

    def ensure(
        self,
        handlers: list[ProvisionHandler],
        env_name: str,
        no_service_check: bool,
    ) -> str | None:
        self.ensure_calls.append((list(handlers), env_name, no_service_check))
        if self._raises is not None:
            raise self._raises
        return self._result


def _make_service_with_check(
    config: WorkspaceConfig,
    service_check: _RecordingServiceCheck,
    execution_svc: _FakeExecutionService | None = None,
) -> tuple[ProvisionService, _FakeExecutionService, _FakeReporter]:
    exec_svc = execution_svc or _FakeExecutionService()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=service_check,  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    return svc, exec_svc, reporter


def test_service_check_ensure_called_before_resource_handlers() -> None:
    """ensure() is called for resource sub-target before any handler executes."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    sc = _RecordingServiceCheck(result="ok")
    _svc, _exec_svc, reporter = _make_service_with_check(config, sc)

    call_order: list[str] = []

    class _TrackingExecSvc:
        calls: ClassVar[list[Any]] = []

        def run_handler(self, handler: Any, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            call_order.append(f"exec:{handler.subtarget}")
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=True)

    orig_ensure = sc.ensure

    def _tracking_ensure(handlers: Any, env_name: str, no_service_check: bool) -> str | None:
        call_order.append("ensure")
        return orig_ensure(handlers, env_name, no_service_check)

    sc.ensure = _tracking_ensure  # type: ignore[method-assign]

    tracking_exec = _TrackingExecSvc()
    svc2 = ProvisionService(
        config=config,
        execution_svc=tracking_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=sc,
        fs=_make_fs_with_env(),
    )
    svc2.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    # ensure must appear before any exec call in the event log
    assert call_order.index("ensure") < call_order.index("exec:resource")


def test_service_check_ensure_not_called_for_dependency_subtarget() -> None:
    """ensure() is still called for dependency sub-target (even though required_services
    is not allowed there); the service check returns None for handlers with no required_services."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    sc = _RecordingServiceCheck(result=None)
    svc, _exec_svc, reporter = _make_service_with_check(config, sc)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    # ensure() is called; it returns None (no required_services) → service_check=None in handler_result
    assert len(sc.ensure_calls) == 1
    assert reporter.handler_result_calls[0]["service_check"] is None


def test_orchestrator_error_in_ensure_aborts_run_cleanly() -> None:
    """A ClickException raised from ensure() propagates and aborts the provision run.

    The ClickException surfaces as a non-zero exit at the CLI boundary (cli.py:192-194).
    At the ProvisionService level it propagates uncaught — that is the intended behavior:
    the handler/command's sys.exit path doesn't apply here; the CLI boundary does.

    This is a whole-run precondition failure (unlike a per-handler
    cwd-resolution failure under clean — see execution_service.py), so it
    legitimately still aborts by propagating. But the reporter must still see
    a closing `provision_finished` event first, or a `--json` consumer is
    left with an unterminated stream (no `finished` line) even though the
    run is over.
    """
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    error = click.ClickException("no service orchestrator registered")
    sc = _RecordingServiceCheck(raises=error)
    svc, exec_svc, reporter = _make_service_with_check(config, sc)

    # ProvisionService itself does not catch the ClickException — it propagates.
    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget="resource",
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
        )  # type: ignore[arg-type]

    assert "no service orchestrator" in exc_info.value.format_message()
    # No handler was executed
    assert len(exec_svc.calls) == 0
    # The stream is still closed with a finished event before the exception
    # propagates, even though the run ended via an exception rather than a
    # normal return.
    assert reporter.provision_finished_calls == [("error", None)]


def test_orchestrator_error_in_ensure_under_clean_still_closes_the_json_stream() -> None:
    """The same close-before-propagate behavior holds for a --json clean run.

    ``winter clean`` always drives ``ProvisionAction.clean`` — this pins the
    exact failure mode from the bug report: a service-check precondition
    failure during a clean run must not leave the `--json` NDJSON stream
    without a `finished` line.
    """
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    error = click.ClickException("no service orchestrator registered")
    sc = _RecordingServiceCheck(raises=error)
    svc, exec_svc, reporter = _make_service_with_check(config, sc)

    with pytest.raises(click.ClickException):
        svc.run(
            ENV_NAME,
            subtarget="resource",
            action=ProvisionAction.clean,
            seed=False,
            no_service_check=False,
            reporter=reporter,
        )  # type: ignore[arg-type]

    assert len(exec_svc.calls) == 0
    assert reporter.provision_finished_calls == [("error", None)]


def test_service_check_result_appears_in_handler_result_event() -> None:
    """The service_check return value is placed verbatim in handler_result events."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    sc = _RecordingServiceCheck(result="started:workspace")
    svc, _exec_svc, reporter = _make_service_with_check(config, sc)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(reporter.handler_result_calls) == 1
    assert reporter.handler_result_calls[0]["service_check"] == "started:workspace"


def test_no_service_check_flag_forwarded_to_ensure() -> None:
    """no_service_check=True is forwarded as-is to ensure()."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    sc = _RecordingServiceCheck(result="skipped")
    svc, _exec_svc, reporter = _make_service_with_check(config, sc)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=True,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert len(sc.ensure_calls) == 1
    _handlers, _env, no_svc_check = sc.ensure_calls[0]
    assert no_svc_check is True


# ---------------------------------------------------------------------------
# M1: Destroy/reset failures surface as error status + non-zero exit
# ---------------------------------------------------------------------------


def test_failing_destroy_produces_error_summary() -> None:
    """A failing --destroy script → summary.status='error', exit_code=1."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "destroy": "scripts/destroy.sh"}],
        }
    )
    _svc, _exec_svc, _reporter = _make_service(config=config)

    # Make the destroy action fail on the resource handler.
    class _FailDestroyExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            ok = action is not ProvisionAction.destroy
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailDestroyExecSvc()
    reporter2 = _FakeReporter()
    svc2 = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc2.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter2,
    )  # type: ignore[arg-type]

    assert summary.status == "error"
    assert summary.exit_code == 1
    assert reporter2.provision_finished_calls == [("error", None)]
    # Only the destroy call was made
    assert len(fail_exec.calls) == 1
    assert fail_exec.calls[0][1] is ProvisionAction.destroy


def test_failing_reset_compose_destroy_does_not_run_apply() -> None:
    """--reset compose mode: if destroy fails, apply is NOT invoked and the run fails."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "destroy": "scripts/destroy.sh"}],
        }
    )

    class _FailDestroyExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            ok = action is not ProvisionAction.destroy
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailDestroyExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert summary.status == "error"
    assert summary.exit_code == 1
    # Only destroy was called — apply was NOT invoked
    assert len(fail_exec.calls) == 1
    assert fail_exec.calls[0][1] is ProvisionAction.destroy
    assert reporter.provision_finished_calls == [("error", None)]


def test_existing_apply_abort_semantics_unchanged() -> None:
    """Apply failure still aborts remaining sub-targets (regression guard)."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )

    class _FailApplyExecSvc:
        def __init__(self) -> None:
            self.calls: list[tuple[ProvisionHandler, ProvisionAction, str]] = []

        def run_handler(self, handler: ProvisionHandler, action: ProvisionAction, env_name: str, sink: Any) -> Any:
            self.calls.append((handler, action, env_name))
            ok = len(self.calls) != 1
            return _FakeHandlerExecutionResult(handler=handler, action=action, ok=ok)

    fail_exec = _FailApplyExecSvc()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=fail_exec,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=NoOpServiceCheck(),
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "aborted"
    assert summary.aborted_at == "dependency"
    assert len(fail_exec.calls) == 1  # resource handler never ran


# ---------------------------------------------------------------------------
# C1: Missing env raises a clean ClickException (no traceback)
# ---------------------------------------------------------------------------


def test_missing_env_raises_click_exception() -> None:
    """A non-existent env raises ClickException with a clear message."""
    import click

    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh"}],
        }
    )
    # fs with NO env directory
    fs = FakeFilesystem()
    svc, exec_svc, reporter = _make_service(config=config, fs=fs)

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            "alpah", subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
        )  # type: ignore[arg-type]

    assert "alpah" in exc_info.value.format_message()
    assert len(exec_svc.calls) == 0  # no handlers ran


def test_valid_env_does_not_raise() -> None:
    """A valid env passes the existence check and runs normally."""
    config = _make_config(provision_raw={})
    svc, _exec_svc, reporter = _make_service(config=config)
    # _make_service uses _make_fs_with_env() which includes WORKSPACE_ROOT/alpha
    summary = svc.run(
        ENV_NAME, subtarget=None, action=ProvisionAction.apply, seed=False, no_service_check=False, reporter=reporter
    )  # type: ignore[arg-type]

    assert summary.status == "ok"


# ---------------------------------------------------------------------------
# C2: Malformed workspace [provision] config → hard failure on provision path
# ---------------------------------------------------------------------------


def test_malformed_workspace_provision_raises_click_exception() -> None:
    """A malformed [provision] table raises ClickException on the provision run path."""
    import click

    # "unknown_subtarget" is not a valid provision sub-target key
    config = _make_config(provision_raw={"unknown_subtarget": [{"scope": "workspace", "apply": "scripts/apply.sh"}]})
    svc, _exec_svc, reporter = _make_service(config=config)

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget=None,
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
        )  # type: ignore[arg-type]

    msg = exc_info.value.format_message()
    assert "provision" in msg.lower() or "unknown_subtarget" in msg


def test_workspace_config_load_does_not_raise_on_malformed_provision() -> None:
    """Config LOAD does not raise on a malformed [provision] table (deferred-parse design).

    Phase 1 established this: the [provision] raw dict is stored as-is at load
    time and only parsed on demand by parse_provision().  This test confirms the
    invariant holds — general commands (ws status, etc.) remain unaffected by a
    bad [provision] entry.
    """
    from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
    from winter_cli.config.workspace import parse_provision

    # WorkspaceConfig stores provision_raw without parsing
    config = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        provision_raw={"unknown_subtarget": [{"scope": "workspace", "apply": "scripts/apply.sh"}]},
    )
    # Construction does NOT raise — the raw dict is accepted as-is
    assert config.provision_raw is not None

    # parse_provision() raises ConfigError on demand
    import pytest

    from winter_cli.core.config_file import ConfigError

    with pytest.raises(ConfigError):
        parse_provision(config, source="project")


# ---------------------------------------------------------------------------
# Named resource selector (--name <scope>.<name>)
# ---------------------------------------------------------------------------


def test_name_selector_apply_runs_only_the_named_entry() -> None:
    """--name workspace.mydb runs only that entry; sibling resources untouched."""
    config = _make_config(
        provision_raw={
            "resource": [
                {"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"},
                {"scope": "workspace", "apply": "scripts/create-bucket.sh", "name": "mybucket"},
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].name == "mydb"
    assert exec_svc.calls[0][1] is ProvisionAction.apply
    # Only the matched sub-target's started event fires.
    assert reporter.subtarget_started_calls == ["resource"]


def test_name_selector_destroy_runs_only_the_named_entry() -> None:
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/create-db.sh",
                    "destroy": "scripts/drop-db.sh",
                    "name": "mydb",
                },
                {
                    "scope": "workspace",
                    "apply": "scripts/create-bucket.sh",
                    "destroy": "scripts/drop-bucket.sh",
                    "name": "mybucket",
                },
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].name == "mydb"
    assert exec_svc.calls[0][1] is ProvisionAction.destroy


def test_name_selector_reset_runs_only_the_named_entry() -> None:
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/create-db.sh",
                    "reset": "scripts/reset-db.sh",
                    "name": "mydb",
                },
                {
                    "scope": "workspace",
                    "apply": "scripts/create-bucket.sh",
                    "reset": "scripts/reset-bucket.sh",
                    "name": "mybucket",
                },
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].name == "mydb"
    assert exec_svc.calls[0][1] is ProvisionAction.reset


def test_name_selector_unknown_name_raises_clean_error() -> None:
    """An unmatched selector aborts with a clear error naming the selector — no silent no-op."""
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget=None,
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            name_selector="workspace.nosuch",
        )  # type: ignore[arg-type]

    assert "workspace.nosuch" in exc_info.value.format_message()
    assert len(exec_svc.calls) == 0


def test_name_selector_unknown_scope_token_raises_clean_error() -> None:
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    svc, _exec_svc, reporter = _make_service(config=config)

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget=None,
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            name_selector="bogus-scope.mydb",
        )  # type: ignore[arg-type]

    assert "bogus-scope" in exc_info.value.format_message()


def test_name_selector_malformed_selector_raises_clean_error() -> None:
    """A selector without the '<scope>.<name>' shape is a clean error, not a crash."""
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    svc, _exec_svc, reporter = _make_service(config=config)

    with pytest.raises(click.ClickException):
        svc.run(
            ENV_NAME,
            subtarget=None,
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            name_selector="mydb",  # no '.' separator
        )  # type: ignore[arg-type]


def test_name_selector_ambiguous_match_raises_clean_error() -> None:
    """Same name declared in the same scope across two manifest sources is ambiguous, not a silent pick."""
    import click

    from winter_cli.modules.workspace.models import StandaloneRepository

    class _FakeExtManifest:
        provision = (
            ProvisionHandler(
                subtarget="resource",
                scope=ProvisionScope.workspace,
                apply=("scripts/ext.sh",),
                source="my-ext",
                name="mydb",
            ),
        )

    fake_repo = StandaloneRepository(name="my-ext", path=WORKSPACE_ROOT / "my-ext")
    manifest_path = WORKSPACE_ROOT / "my-ext" / "winter-ext.toml"

    fs = FakeFilesystem(files={manifest_path: ""}, directories=[WORKSPACE_ROOT / ENV_NAME])
    loader = _FakeManifestLoader(manifests={manifest_path: _FakeExtManifest()})
    repo_factory = _FakeRepoFactory(standalone=[fake_repo])

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    exec_svc = _FakeExecutionService()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=loader,  # type: ignore[arg-type]
        repo_factory=repo_factory,  # type: ignore[arg-type]
        service_check=NoOpServiceCheck(),
        fs=fs,
    )

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget=None,
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            name_selector="workspace.mydb",
        )  # type: ignore[arg-type]

    assert "workspace.mydb" in exc_info.value.format_message()
    assert len(exec_svc.calls) == 0


def test_name_selector_same_name_different_scope_disambiguates() -> None:
    """Same short name declared under two different scopes resolves unambiguously by scope token."""
    config = _make_config(
        provision_raw={
            "resource": [
                {"scope": "workspace", "apply": "scripts/ws.sh", "name": "mydb"},
                {"scope": "feature-environment", "apply": "scripts/env.sh", "name": "mydb"},
            ],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="feature.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].scope == ProvisionScope.feature_environment
    assert exec_svc.calls[0][0].apply == ("scripts/env.sh",)


def test_name_selector_works_without_explicit_stage() -> None:
    """--name resolves and runs without requiring --stage."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh", "name": "depname"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "name": "resname"}],
        }
    )
    svc, exec_svc, reporter = _make_service(config=config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        name_selector="workspace.resname",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(exec_svc.calls) == 1
    assert exec_svc.calls[0][0].name == "resname"
    assert reporter.subtarget_started_calls == ["resource"]


def test_name_selector_mismatched_explicit_stage_raises_clean_error() -> None:
    """--name resolving to a different sub-target than an explicit --stage is a clean error."""
    import click

    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "name": "resname"}],
        }
    )
    svc, _exec_svc, reporter = _make_service(config=config)

    with pytest.raises(click.ClickException) as exc_info:
        svc.run(
            ENV_NAME,
            subtarget="data",
            action=ProvisionAction.apply,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            name_selector="workspace.resname",
        )  # type: ignore[arg-type]

    assert "resname" in exc_info.value.format_message() or "workspace.resname" in exc_info.value.format_message()


# ---------------------------------------------------------------------------
# `started` event action key
# ---------------------------------------------------------------------------


def test_started_event_carries_the_run_action() -> None:
    """provision_started's action argument is the run's ProvisionAction value."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    svc, _exec_svc, reporter = _make_service(config=config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
    )  # type: ignore[arg-type]

    assert reporter.provision_started_calls == [(ENV_NAME, ["dependency"], ProvisionAction.clean)]


def test_json_started_event_includes_action_key() -> None:
    """--json started event includes the additive `action` key."""
    import json

    from winter_cli.modules.provision.provision_reporter import JsonProvisionReporter

    class _CapturingClick:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def echo(self, msg: str, err: bool = False) -> None:
            self.lines.append(msg)

    capturing_click = _CapturingClick()
    json_reporter = JsonProvisionReporter(click=capturing_click)

    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    svc, _exec_svc, _reporter = _make_service(config=config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
    )

    events = [json.loads(line) for line in capturing_click.lines]
    started_events = [e for e in events if e.get("type") == "started"]
    assert len(started_events) == 1
    assert started_events[0]["action"] == "clean"
    assert started_events[0]["subtargets"] == ["dependency"]


def test_stream_provision_started_uses_clean_verb() -> None:
    """StreamProvisionReporter prints 'Cleaning' rather than 'Provisioning' for a clean run."""
    from winter_cli.modules.provision.provision_reporter import StreamProvisionReporter

    class _CapturingClick:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def echo(self, msg: str, err: bool = False) -> None:
            self.lines.append(msg)

        def style(self, msg: str, **kwargs: Any) -> str:
            return msg

    capturing_click = _CapturingClick()
    stream_reporter = StreamProvisionReporter(click=capturing_click)
    stream_reporter.provision_started(ENV_NAME, ["dependency"], ProvisionAction.clean)

    assert capturing_click.lines == [f"Cleaning {ENV_NAME!r}: dependency"]


def test_stream_provision_started_uses_the_right_verb_for_every_action() -> None:
    """Every ProvisionAction member maps to its own verb — no silent 'Provisioning' fallback."""
    from winter_cli.modules.provision.provision_reporter import StreamProvisionReporter

    class _CapturingClick:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def echo(self, msg: str, err: bool = False) -> None:
            self.lines.append(msg)

        def style(self, msg: str, **kwargs: Any) -> str:
            return msg

    expected = {
        ProvisionAction.apply: "Provisioning",
        ProvisionAction.destroy: "Destroying",
        ProvisionAction.reset: "Resetting",
        ProvisionAction.clean: "Cleaning",
    }
    for action, verb in expected.items():
        capturing_click = _CapturingClick()
        stream_reporter = StreamProvisionReporter(click=capturing_click)
        stream_reporter.provision_started(ENV_NAME, ["dependency"], action)
        assert capturing_click.lines == [f"{verb} {ENV_NAME!r}: dependency"]
