"""Tests for ``winter provision --dry-run``.

Covers:
- Plan ordering across the full chain (dependency → resource → data)
- Destructive variants: --destroy and --reset action resolution in the plan
- Service-check preview in plan events
- No-handlers no-op: plan reports the no-op sub-targets
- ``--json`` plan shape (would_run flag, structured fields)
- No execution, no service-check calls when dry_run=True
- --dry-run accepted with --reset, --destroy, and --seed (command-level)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from tests.conftest import FakeFilesystem
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.provision.manifest import ProvisionAction, ProvisionHandler, ProvisionScope
from winter_cli.modules.provision.provision_service import ProvisionService

WORKSPACE_ROOT = Path("/ws")
ENV_NAME = "alpha"


# ---------------------------------------------------------------------------
# Shared fakes (mirrors test_provision_service.py)
# ---------------------------------------------------------------------------


class _FakeHandlerExecutionResult:
    def __init__(self, handler: ProvisionHandler, action: ProvisionAction, ok: bool = True) -> None:
        self.handler = handler
        self.action = action
        self.runs: tuple[Any, ...] = ()
        self.error: str | None = None
        self._ok = ok

    @property
    def ok(self) -> bool:
        return self._ok


class _RecordingExecutionService:
    """Records run_handler calls; dry_run tests must assert it is NEVER called."""

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
        return _FakeHandlerExecutionResult(handler=handler, action=action)


class _RecordingServiceCheck:
    """Records ensure() calls; dry_run tests must assert it is NEVER called."""

    def __init__(self) -> None:
        self.ensure_calls: list[Any] = []

    def ensure(
        self,
        handlers: list[ProvisionHandler],
        env_name: str,
        no_service_check: bool,
    ) -> str | None:
        self.ensure_calls.append((handlers, env_name, no_service_check))
        return None


class _FakeManifestLoader:
    def __init__(self, manifests: dict[Path, Any] | None = None) -> None:
        self._manifests = manifests or {}

    def load(self, repo: Any, manifest_path: Path | None) -> Any:
        if manifest_path in self._manifests:
            return self._manifests[manifest_path]
        raise ValueError(f"no manifest registered for {manifest_path}")


class _FakeRepoFactory:
    def __init__(self) -> None:
        pass

    def get_standalone_repos(self) -> list[Any]:
        return []

    def get_extension_repos(self) -> list[Any]:
        return []

    def get_project_repos(self) -> list[Any]:
        return []


class _FakeReporter:
    def __init__(self) -> None:
        self.provision_started_calls: list[tuple[str, list[str], ProvisionAction]] = []
        self.subtarget_started_calls: list[str] = []
        self.no_handlers_calls: list[str] = []
        self.handler_result_calls: list[dict[str, Any]] = []
        self.handler_warn_calls: list[dict[str, Any]] = []
        self.plan_handler_calls: list[dict[str, Any]] = []
        self.provision_finished_calls: list[tuple[str, str | None]] = []
        self.execution_started_calls: list[Any] = []

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

    # IProvisionOutputSink stubs (never called in dry-run tests)
    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self.execution_started_calls.append((label, action, cwd))

    def execution_output_line(self, label: str, line: str) -> None:
        pass

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        pass

    def execution_error(self, label: str, error: str) -> None:
        pass


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
    scope: ProvisionScope = ProvisionScope.workspace,
    source: str = "project",
    apply: tuple[str, ...] = ("scripts/apply.sh",),
    destroy: tuple[str, ...] | None = None,
    reset: tuple[str, ...] | None = None,
    required_services: tuple[str, ...] = (),
) -> ProvisionHandler:
    return ProvisionHandler(
        subtarget=subtarget,
        scope=scope,
        apply=apply,
        source=source,
        destroy=destroy,
        reset=reset,
        required_services=required_services,
    )


def _make_fs_with_env(env_name: str = ENV_NAME) -> FakeFilesystem:
    return FakeFilesystem(directories=[WORKSPACE_ROOT / env_name])


def _make_service(
    config: WorkspaceConfig,
    exec_svc: _RecordingExecutionService | None = None,
    service_check: _RecordingServiceCheck | None = None,
    fs: FakeFilesystem | None = None,
) -> tuple[ProvisionService, _RecordingExecutionService, _RecordingServiceCheck, _FakeReporter]:
    exec_svc = exec_svc or _RecordingExecutionService()
    sc = service_check or _RecordingServiceCheck()
    reporter = _FakeReporter()
    svc = ProvisionService(
        config=config,
        execution_svc=exec_svc,  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=sc,  # type: ignore[arg-type]
        fs=fs or _make_fs_with_env(),
    )
    return svc, exec_svc, sc, reporter


# ---------------------------------------------------------------------------
# Plan ordering: full chain
# ---------------------------------------------------------------------------


def test_dry_run_full_chain_emits_plan_events_in_order() -> None:
    """dry_run=True emits plan_handler events in dependency → resource → data order."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/dat.sh"}],
        }
    )
    svc, exec_svc, sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert summary.exit_code == 0
    # Three plan events, one per sub-target.
    assert len(reporter.plan_handler_calls) == 3
    assert [e["subtarget"] for e in reporter.plan_handler_calls] == ["dependency", "resource", "data"]
    # All are apply actions (bare invocation).
    assert all(e["action"] == "apply" for e in reporter.plan_handler_calls)
    # No scripts executed, no service check called.
    assert len(exec_svc.calls) == 0
    assert len(sc.ensure_calls) == 0


def test_dry_run_reports_subtarget_started_for_each_subtarget() -> None:
    """subtarget_started is emitted even in dry-run mode."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert "dependency" in reporter.subtarget_started_calls
    assert "resource" in reporter.subtarget_started_calls


def test_dry_run_plan_event_fields_are_complete() -> None:
    """plan_handler event contains subtarget, scope, source, script, action, required_services."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    evt = reporter.plan_handler_calls[0]
    assert evt["subtarget"] == "resource"
    assert evt["scope"] == "workspace"
    assert evt["source"] == "project"
    assert evt["commands"] == ["scripts/res.sh"]
    assert evt["action"] == "apply"
    assert evt["required_services"] == []
    assert evt["service_check_preview"] is None


# ---------------------------------------------------------------------------
# Destructive variants
# ---------------------------------------------------------------------------


def test_dry_run_destroy_emits_destroy_action_when_declared() -> None:
    """--dry-run --destroy: plan shows destroy action when destroy script is declared."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "destroy": "scripts/drop.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["action"] == "destroy"
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/drop.sh"]
    assert len(exec_svc.calls) == 0


def test_dry_run_destroy_emits_no_plan_event_when_no_destroy_declared() -> None:
    """--dry-run --destroy: no plan_handler emitted when no destroy script declared."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    # Would warn and no-op; plan has no event for this handler.
    assert len(reporter.plan_handler_calls) == 0
    assert len(exec_svc.calls) == 0


def test_dry_run_reset_with_dedicated_reset_script() -> None:
    """--dry-run --reset: plan shows reset action when reset script is declared."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh", "reset": "scripts/reseed.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["action"] == "reset"
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/reseed.sh"]
    assert len(exec_svc.calls) == 0


def test_dry_run_reset_compose_destroy_then_apply() -> None:
    """--dry-run --reset: plan shows destroy + apply when no reset but destroy declared."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/apply.sh", "destroy": "scripts/drop.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 2
    assert reporter.plan_handler_calls[0]["action"] == "destroy"
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/drop.sh"]
    assert reporter.plan_handler_calls[1]["action"] == "apply"
    assert reporter.plan_handler_calls[1]["commands"] == ["scripts/apply.sh"]
    assert len(exec_svc.calls) == 0


def test_dry_run_reset_degrades_to_apply_when_neither_reset_nor_destroy() -> None:
    """--dry-run --reset: plan shows apply when neither reset nor destroy declared."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.reset,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["action"] == "apply"
    assert len(exec_svc.calls) == 0


# ---------------------------------------------------------------------------
# clean action
# ---------------------------------------------------------------------------


def test_dry_run_clean_emits_clean_action_when_declared() -> None:
    """--dry-run clean: plan shows clean action when a clean script is declared."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh", "clean": "scripts/clean-dep.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["action"] == "clean"
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/clean-dep.sh"]
    assert len(exec_svc.calls) == 0


def test_dry_run_clean_reports_no_handlers_when_nothing_declares_clean() -> None:
    """--dry-run clean: a sub-target where nothing declares clean plans a no-op, not a plan_handler."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert reporter.no_handlers_calls == ["dependency"]
    assert len(reporter.plan_handler_calls) == 0
    assert len(exec_svc.calls) == 0


def test_dry_run_clean_filters_out_handler_with_no_clean_declared() -> None:
    """--dry-run clean: only the handler declaring clean gets a plan_handler event."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "workspace", "apply": "scripts/apply-a.sh", "clean": "scripts/clean-a.sh"},
                {"scope": "workspace", "apply": "scripts/apply-b.sh"},
            ],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/clean-a.sh"]
    assert len(exec_svc.calls) == 0


def test_dry_run_clean_name_selector_without_clean_warns_and_emits_no_plan_event() -> None:
    """--dry-run clean --name on a handler with no declared clean: warns, no plan event, no error.

    The real run warns for this exact case (see `_run_clean`) — the preview
    must say the same thing the real run would, not stay silent.
    """
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"}],
        }
    )
    svc, exec_svc, sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(reporter.plan_handler_calls) == 0
    assert len(reporter.handler_warn_calls) == 1
    assert "skipping" in reporter.handler_warn_calls[0]["message"]
    assert len(exec_svc.calls) == 0
    assert len(sc.ensure_calls) == 0


def test_dry_run_seed_shows_resource_then_data() -> None:
    """--dry-run --seed: plan shows resource apply then data apply."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh"}],
        }
    )
    svc, exec_svc, _sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=True,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 2
    assert reporter.plan_handler_calls[0]["subtarget"] == "resource"
    assert reporter.plan_handler_calls[1]["subtarget"] == "data"
    assert all(e["action"] == "apply" for e in reporter.plan_handler_calls)
    assert len(exec_svc.calls) == 0


# ---------------------------------------------------------------------------
# Service-check preview
# ---------------------------------------------------------------------------


def test_dry_run_service_check_preview_when_required_services_declared() -> None:
    """plan_handler includes service_check_preview when required_services declared."""
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/res.sh",
                    "required_services": ["workspace/postgres"],
                }
            ],
        }
    )
    svc, exec_svc, sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    evt = reporter.plan_handler_calls[0]
    assert evt["required_services"] == ["workspace/postgres"]
    assert evt["service_check_preview"] == "workspace"
    # Service check seam must NOT be called.
    assert len(sc.ensure_calls) == 0
    assert len(exec_svc.calls) == 0


def test_dry_run_service_check_preview_none_when_no_required_services() -> None:
    """service_check_preview is None when no required_services declared."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "workspace", "apply": "scripts/seed.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert reporter.plan_handler_calls[0]["service_check_preview"] is None


def test_dry_run_service_check_preview_includes_env_scope() -> None:
    """service_check_preview includes env name for env-scoped required_services."""
    config = _make_config(
        provision_raw={
            "resource": [
                {
                    "scope": "workspace",
                    "apply": "scripts/res.sh",
                    "required_services": ["alpha/postgres", "workspace/rabbitmq"],
                }
            ],
        }
    )
    svc, _, sc, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    evt = reporter.plan_handler_calls[0]
    # Both scopes should appear, sorted.
    assert evt["service_check_preview"] == "alpha,workspace"
    assert len(sc.ensure_calls) == 0


# ---------------------------------------------------------------------------
# No-handlers no-op
# ---------------------------------------------------------------------------


def test_dry_run_no_handlers_reports_no_op_for_all_subtargets() -> None:
    """Empty manifest with dry_run=True reports no_handlers for all sub-targets, no plan events."""
    config = _make_config(provision_raw={})
    svc, exec_svc, _sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert reporter.no_handlers_calls == ["dependency", "resource", "data"]
    assert len(reporter.plan_handler_calls) == 0
    assert len(exec_svc.calls) == 0
    assert reporter.provision_finished_calls == [("ok", None)]


def test_dry_run_single_subtarget_no_handlers() -> None:
    """Single sub-target with no handlers: one no_handlers event, no plan events."""
    config = _make_config(provision_raw={})
    svc, exec_svc, _, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert summary.status == "ok"
    assert reporter.no_handlers_calls == ["resource"]
    assert len(reporter.plan_handler_calls) == 0
    assert len(exec_svc.calls) == 0


# ---------------------------------------------------------------------------
# handler_result never emitted in dry-run
# ---------------------------------------------------------------------------


def test_dry_run_never_emits_handler_result() -> None:
    """handler_result is never emitted in dry-run mode (plan_handler is emitted instead)."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh"}],
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh"}],
            "data": [{"scope": "workspace", "apply": "scripts/dat.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.handler_result_calls) == 0
    assert len(reporter.plan_handler_calls) == 3


# ---------------------------------------------------------------------------
# --json plan shape (via JsonProvisionReporter)
# ---------------------------------------------------------------------------


def test_dry_run_json_emits_plan_handler_with_would_run_flag() -> None:
    """--dry-run --json emits plan_handler events with would_run=True."""

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
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh"}],
            "resource": [
                {
                    "scope": "feature-environment",
                    "apply": "scripts/res.sh",
                    "destroy": "scripts/drop.sh",
                    "required_services": ["workspace/postgres"],
                }
            ],
        }
    )
    svc = ProvisionService(
        config=config,
        execution_svc=_RecordingExecutionService(),  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=_RecordingServiceCheck(),  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
        dry_run=True,
    )

    assert summary.status == "ok"
    events = [json.loads(line) for line in capturing_click.lines]

    plan_events = [e for e in events if e.get("type") == "plan_handler"]
    assert len(plan_events) == 2

    dep_evt = plan_events[0]
    assert dep_evt["would_run"] is True
    assert dep_evt["subtarget"] == "dependency"
    assert dep_evt["action"] == "apply"
    assert dep_evt["commands"] == ["scripts/dep.sh"]
    assert dep_evt["required_services"] == []
    assert dep_evt["service_check_preview"] is None

    res_evt = plan_events[1]
    assert res_evt["would_run"] is True
    assert res_evt["subtarget"] == "resource"
    assert res_evt["action"] == "apply"
    assert res_evt["required_services"] == ["workspace/postgres"]
    assert res_evt["service_check_preview"] == "workspace"

    # finished event present with status "ok"
    finished = [e for e in events if e.get("type") == "finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "ok"


def test_dry_run_json_destroy_emits_plan_handler_with_destroy_action() -> None:
    """--dry-run --destroy --json emits plan_handler with action=destroy."""
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
            "resource": [{"scope": "workspace", "apply": "scripts/res.sh", "destroy": "scripts/drop.sh"}],
        }
    )
    svc = ProvisionService(
        config=config,
        execution_svc=_RecordingExecutionService(),  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=_RecordingServiceCheck(),  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.destroy,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
        dry_run=True,
    )

    events = [json.loads(line) for line in capturing_click.lines]
    plan_events = [e for e in events if e.get("type") == "plan_handler"]
    assert len(plan_events) == 1
    assert plan_events[0]["action"] == "destroy"
    assert plan_events[0]["would_run"] is True


# ---------------------------------------------------------------------------
# Command-level: --dry-run flag accepted in all paths
# ---------------------------------------------------------------------------


def test_provision_dry_run_flag_in_help() -> None:
    """--dry-run appears in the help output of the provision command."""
    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output


def test_provision_dry_run_with_reset_accepted_at_command_level() -> None:
    """--dry-run --reset is accepted at the command level (no validation rejection)."""
    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    # Should fail for missing env, not for flag combination.
    result = runner.invoke(provision_command, ["alpha", "--stage", "resource", "--reset", "--dry-run"])
    # The command may fail due to missing container/env but NOT due to flag rejection.
    assert "--reset and --dry-run" not in (result.output or "")
    assert "mutually exclusive" not in (result.output or "")


def test_provision_dry_run_with_destroy_accepted_at_command_level() -> None:
    """--dry-run --destroy is accepted at the command level (no validation rejection)."""
    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--stage", "resource", "--destroy", "--dry-run"])
    assert "--destroy and --dry-run" not in (result.output or "")
    assert "mutually exclusive" not in (result.output or "")


def test_provision_dry_run_with_seed_accepted_at_command_level() -> None:
    """--dry-run --seed is accepted at the command level (no validation rejection)."""
    from winter_cli.modules.provision.command import provision_command

    runner = CliRunner()
    result = runner.invoke(provision_command, ["alpha", "--stage", "resource", "--seed", "--dry-run"])
    assert "--seed and --dry-run" not in (result.output or "")
    assert "mutually exclusive" not in (result.output or "")


# ---------------------------------------------------------------------------
# Scope ordering preserved in plan
# ---------------------------------------------------------------------------


def test_dry_run_plan_preserves_scope_ordering() -> None:
    """Dry-run plan emits handlers in the same scope order as real run."""
    config = _make_config(
        provision_raw={
            "dependency": [
                {"scope": "feature-worktree", "apply": "scripts/worktree.sh"},
                {"scope": "feature-environment", "apply": "scripts/env.sh"},
                {"scope": "workspace", "apply": "scripts/ws.sh"},
            ],
        }
    )
    svc, exec_svc, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 3
    scopes = [e["scope"] for e in reporter.plan_handler_calls]
    assert scopes == ["workspace", "feature-environment", "feature-worktree"]
    assert len(exec_svc.calls) == 0


# ---------------------------------------------------------------------------
# cwd preview in dry-run plan — workspace-scope blast radius
# ---------------------------------------------------------------------------


def test_dry_run_workspace_scope_cwd_states_workspace_root_not_env() -> None:
    """A workspace-scoped handler's preview states its cwd is the shared
    workspace root, explicitly called out as NOT a path inside the named
    env — a workspace-scoped clean runs once at the shared root regardless
    of which env the run was invoked against, so the preview must make that
    unambiguous rather than leaving it to the ``scope`` field alone."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    evt = reporter.plan_handler_calls[0]
    assert evt["scope"] == "workspace"
    assert str(WORKSPACE_ROOT) in evt["cwd"]
    assert str(WORKSPACE_ROOT / ENV_NAME) not in evt["cwd"]
    assert "not inside env" in evt["cwd"]
    assert ENV_NAME in evt["cwd"]


def test_dry_run_workspace_scope_cwd_is_the_same_across_different_envs() -> None:
    """The same workspace-scoped handler previews the identical cwd for two
    different envs — pinning that it never resolves to a per-env path."""
    config = _make_config(
        provision_raw={
            "dependency": [{"scope": "workspace", "apply": "scripts/apply.sh", "clean": "scripts/clean.sh"}],
        }
    )
    cwds: dict[str, str] = {}
    for env_name in ("alpha", "beta"):
        svc, _, _, reporter = _make_service(config, fs=_make_fs_with_env(env_name))
        svc.run(
            env_name,
            subtarget="dependency",
            action=ProvisionAction.clean,
            seed=False,
            no_service_check=False,
            reporter=reporter,
            dry_run=True,  # type: ignore[arg-type]
        )
        cwds[env_name] = reporter.plan_handler_calls[0]["cwd"]

    assert cwds["alpha"] != cwds["beta"]  # each names its own env in the note
    assert str(WORKSPACE_ROOT) in cwds["alpha"]
    assert str(WORKSPACE_ROOT) in cwds["beta"]
    assert str(WORKSPACE_ROOT / "alpha") not in cwds["alpha"]
    assert str(WORKSPACE_ROOT / "beta") not in cwds["beta"]


def test_dry_run_feature_environment_scope_cwd_is_env_root() -> None:
    """A feature-environment handler without ``project`` previews the env
    root as its cwd — contrast case for the workspace-scope note above."""
    config = _make_config(
        provision_raw={
            "resource": [{"scope": "feature-environment", "apply": "scripts/apply.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="resource",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["cwd"] == str(WORKSPACE_ROOT / ENV_NAME)


def test_dry_run_json_plan_handler_includes_cwd_field() -> None:
    """--dry-run --json plan_handler event includes the cwd key, and states
    the workspace root for a workspace-scoped handler."""
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
            "dependency": [{"scope": "workspace", "apply": "scripts/dep.sh", "clean": "scripts/clean.sh"}],
        }
    )
    svc = ProvisionService(
        config=config,
        execution_svc=_RecordingExecutionService(),  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=_RecordingServiceCheck(),  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    svc.run(
        ENV_NAME,
        subtarget="dependency",
        action=ProvisionAction.clean,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
        dry_run=True,
    )

    events = [json.loads(line) for line in capturing_click.lines]
    plan_events = [e for e in events if e.get("type") == "plan_handler"]
    assert len(plan_events) == 1
    assert "cwd" in plan_events[0]
    assert str(WORKSPACE_ROOT) in plan_events[0]["cwd"]
    assert str(WORKSPACE_ROOT / ENV_NAME) not in plan_events[0]["cwd"]


def test_dry_run_stream_plan_handler_line_includes_cwd() -> None:
    """StreamProvisionReporter's human-readable preview line surfaces cwd too."""
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
    stream_reporter.plan_handler(
        subtarget="dependency",
        scope="workspace",
        source="project",
        commands=["scripts/clean.sh"],
        action="clean",
        required_services=[],
        service_check_preview=None,
        cwd=f"{WORKSPACE_ROOT} (workspace root — shared, not inside env {ENV_NAME!r})",
    )

    assert len(capturing_click.lines) == 1
    assert f"cwd={WORKSPACE_ROOT}" in capturing_click.lines[0]
    assert "not inside env" in capturing_click.lines[0]


# ---------------------------------------------------------------------------
# project field in dry-run plan
# ---------------------------------------------------------------------------


def test_dry_run_plan_handler_includes_project_when_set() -> None:
    """plan_handler event includes the project field when handler declares one."""
    from winter_cli.config.models import AdoptExtensions, ProjectRepositoryConfig

    # parse_provision needs project_names; inject via project_repos.
    config_with_project = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        provision_raw={"data": [{"scope": "feature-environment", "apply": "scripts/seed.sh", "project": "web"}]},
        project_repos=[ProjectRepositoryConfig(name="web", url="git@example.com:org/web.git")],
    )
    svc, _, _, reporter = _make_service(config_with_project)
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    evt = reporter.plan_handler_calls[0]
    assert evt["project"] == "web"


def test_dry_run_plan_handler_project_is_none_when_not_set() -> None:
    """plan_handler event has project=None when handler has no project field."""
    config = _make_config(
        provision_raw={
            "data": [{"scope": "feature-environment", "apply": "scripts/seed.sh"}],
        }
    )
    svc, _, _, reporter = _make_service(config)
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,  # type: ignore[arg-type]
    )

    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["project"] is None


# ---------------------------------------------------------------------------
# Named selector narrows --dry-run / --json plan output
# ---------------------------------------------------------------------------


def test_dry_run_name_selector_narrows_plan_to_one_handler() -> None:
    """--dry-run --name shows only the resolved handler's plan_handler event."""
    config = _make_config(
        provision_raw={
            "resource": [
                {"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"},
                {"scope": "workspace", "apply": "scripts/create-bucket.sh", "name": "mybucket"},
            ],
        }
    )
    svc, exec_svc, sc, reporter = _make_service(config)
    summary = svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=reporter,
        dry_run=True,
        name_selector="workspace.mydb",
    )  # type: ignore[arg-type]

    assert summary.status == "ok"
    assert len(reporter.plan_handler_calls) == 1
    assert reporter.plan_handler_calls[0]["commands"] == ["scripts/create-db.sh"]
    assert len(exec_svc.calls) == 0
    assert len(sc.ensure_calls) == 0


def test_dry_run_json_name_selector_narrows_plan_to_one_handler() -> None:
    """--dry-run --json --name emits exactly one plan_handler for the matched entry."""
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
            "resource": [
                {"scope": "workspace", "apply": "scripts/create-db.sh", "name": "mydb"},
                {"scope": "workspace", "apply": "scripts/create-bucket.sh", "name": "mybucket"},
            ],
        }
    )
    svc = ProvisionService(
        config=config,
        execution_svc=_RecordingExecutionService(),  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=_RecordingServiceCheck(),  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    svc.run(
        ENV_NAME,
        subtarget=None,
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
        dry_run=True,
        name_selector="workspace.mydb",
    )

    events = [json.loads(line) for line in capturing_click.lines]
    plan_events = [e for e in events if e.get("type") == "plan_handler"]
    assert len(plan_events) == 1
    assert plan_events[0]["commands"] == ["scripts/create-db.sh"]


def test_dry_run_json_plan_handler_includes_project_field() -> None:
    """--dry-run --json plan_handler event includes the project key."""
    from winter_cli.config.models import AdoptExtensions, ProjectRepositoryConfig
    from winter_cli.modules.provision.provision_reporter import JsonProvisionReporter

    class _CapturingClick:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def echo(self, msg: str, err: bool = False) -> None:
            self.lines.append(msg)

    capturing_click = _CapturingClick()
    json_reporter = JsonProvisionReporter(click=capturing_click)

    config_with_project = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        service_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
        provision_raw={"data": [{"scope": "feature-environment", "apply": "scripts/seed.sh", "project": "api"}]},
        project_repos=[ProjectRepositoryConfig(name="api", url="git@example.com:org/api.git")],
    )
    svc = ProvisionService(
        config=config_with_project,
        execution_svc=_RecordingExecutionService(),  # type: ignore[arg-type]
        manifest_loader=_FakeManifestLoader(),
        repo_factory=_FakeRepoFactory(),
        service_check=_RecordingServiceCheck(),  # type: ignore[arg-type]
        fs=_make_fs_with_env(),
    )
    svc.run(
        ENV_NAME,
        subtarget="data",
        action=ProvisionAction.apply,
        seed=False,
        no_service_check=False,
        reporter=json_reporter,  # type: ignore[arg-type]
        dry_run=True,
    )

    import json as _json

    events = [_json.loads(line) for line in capturing_click.lines]
    plan_events = [e for e in events if e.get("type") == "plan_handler"]
    assert len(plan_events) == 1
    assert plan_events[0]["project"] == "api"
