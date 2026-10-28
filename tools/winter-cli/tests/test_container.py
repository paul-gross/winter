from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from dependency_injector import providers

from tests.conftest import FakeOperationTracer, FakeSessionTracer
from tests.modules.workspace.conftest import add_env_worktree, init_project
from winter_cli.container import Container, FreshInitServiceFactory, LazyTarget
from winter_cli.modules.workspace import dashboard_snapshot_service
from winter_cli.modules.workspace.agent_install import ExtensionAgentService
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.extension_agentsmd_service import ExtensionAgentsMdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.read_workspace_repository import ReadWorkspaceRepository
from winter_cli.modules.workspace.internal.subprocess_command_entry_runner import SubprocessCommandEntryRunner
from winter_cli.modules.workspace.models import FeatureEnvironment
from winter_cli.modules.workspace.prune_service import PruneService
from winter_cli.modules.workspace.workspace_push_service import WorkspacePushService
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService


def test_container_resolves_workspace_services_end_to_end(container: Container) -> None:
    """DI wiring boots without errors and yields fully constructed services.

    Resolving each top-level workspace service forces the container to walk
    the full provider graph (workspace_config_svc → workspace_config →
    repo_repo → workspace → repo_factory → worktree_repo → git_ops_svc →
    each service). If any provider's dependencies drift from the constructor
    signature, this test fails at construction time — long before a CLI
    invocation would notice.
    """
    assert isinstance(container.env_status_svc(), EnvStatusService)
    assert isinstance(container.workspace_sync_svc(), WorkspaceSyncService)
    assert isinstance(container.workspace_push_svc(), WorkspacePushService)
    assert isinstance(container.env_checkout_svc(), EnvCheckoutService)
    assert isinstance(container.workspace_snapshot_svc(), WorkspaceSnapshotService)


def test_container_resolves_every_top_level_service(container: Container) -> None:
    """Smoke-test every singleton/factory service the CLI commands depend on."""
    assert isinstance(container.init_svc(), InitService)
    assert isinstance(container.prune_svc(), PruneService)
    assert isinstance(container.drift_warning_svc(), DriftWarningService)
    assert isinstance(container.extension_symlink_svc(), ExtensionSymlinkService)
    assert isinstance(container.extension_agent_svc(), ExtensionAgentService)
    assert isinstance(container.extension_hook_svc(), ExtensionHookService)
    assert isinstance(container.extension_exclude_svc(), ExtensionExcludeService)
    assert isinstance(container.extension_agentsmd_svc(), ExtensionAgentsMdService)


def test_container_resolves_service_handler(container: Container) -> None:
    """The `winter service` dispatch chain wires end-to-end (lazy providers resolve)."""
    from winter_cli.modules.service.handler import ServiceHandler

    assert isinstance(container.service_handler(), ServiceHandler)


def test_container_resolves_capabilities_handler(container: Container) -> None:
    """The `winter capabilities` dispatch chain wires end-to-end (lazy providers resolve)."""
    from winter_cli.modules.capability.handler import CapabilitiesHandler

    assert isinstance(container.capabilities_handler(), CapabilitiesHandler)


def test_container_resolves_command_entry_runner_and_env_provisioner(container: Container) -> None:
    """The command-band execution seam and its resolver/provisioner consumers wire end-to-end."""
    from winter_cli.modules.workspace.env_band_resolver_service import EnvBandResolverService
    from winter_cli.modules.workspace.env_provisioner import EnvProvisionerService

    assert isinstance(container.command_entry_runner(), SubprocessCommandEntryRunner)
    assert isinstance(container.env_band_resolver(), EnvBandResolverService)
    assert isinstance(container.env_provisioner(), EnvProvisionerService)


def test_container_injects_the_nested_workspace_service_into_init_destroy_and_status(container: Container) -> None:
    """The nested runner runs `winter` from PATH, and one service instance reaches every nested call site."""
    from winter_cli.config.internal.tomlkit_local_overlay_repository import TomlkitLocalOverlayRepository
    from winter_cli.modules.workspace.destroy_service import DestroyService
    from winter_cli.modules.workspace.internal.subprocess_nested_workspace_runner import (
        SubprocessNestedWorkspaceRunner,
    )
    from winter_cli.modules.workspace.nested_workspace_service import NestedWorkspaceService

    runner = container.nested_workspace_runner()
    assert isinstance(runner, SubprocessNestedWorkspaceRunner)
    assert runner._command == ("winter",)
    nested_svc = container.nested_workspace_svc()
    assert isinstance(nested_svc, NestedWorkspaceService)
    assert nested_svc._workspace_root == container.workspace_config().workspace_root
    destroy_svc = container.destroy_svc()
    assert isinstance(destroy_svc, DestroyService)
    for svc in (container.init_svc(), destroy_svc, container.workspace_snapshot_svc()):
        assert svc._nested_svc is nested_svc
    assert nested_svc._runner is runner
    overlay_repo = container.nested_workspace_svc()._overlay_repo
    assert isinstance(overlay_repo, TomlkitLocalOverlayRepository)


def test_container_resolves_restack_providers(container: Container) -> None:
    """The `winter ws restack` dispatch chain wires end-to-end: both services
    and the handler that composes them resolve through the full DI graph."""
    from winter_cli.modules.workspace.handlers.restack_handler import RestackHandler

    assert isinstance(container.env_restack_plan_svc(), EnvRestackPlanService)
    assert isinstance(container.env_restack_svc(), EnvRestackService)
    assert isinstance(container.restack_handler(), RestackHandler)


def test_container_binds_the_noop_command_tracer_by_default(container: Container) -> None:
    from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
    from winter_cli.core.tracing import TracingSettings

    assert isinstance(container.command_tracer(), NoopCommandTracer)
    assert container.command_tracer() is container.command_tracer()
    assert container.tracing_settings() == TracingSettings()


def test_container_binds_the_opentelemetry_tracer_when_an_endpoint_is_set(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry import trace

    from winter_cli.core.internal.otel_command_tracer import OtelCommandTracer
    from winter_cli.core.tracing import TracingSettings

    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace._TRACER_PROVIDER_SET_ONCE, "_done", False)
    container.tracing_settings.override(providers.Object(TracingSettings(otlp_endpoint="http://localhost:4318")))

    tracer = container.command_tracer()

    assert isinstance(tracer, OtelCommandTracer)
    assert container.command_tracer() is tracer


def test_container_keeps_the_noop_tracer_when_the_sdk_is_disabled(container: Container) -> None:
    from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
    from winter_cli.core.tracing import TracingSettings

    container.tracing_settings.override(
        providers.Object(TracingSettings(otlp_endpoint="http://localhost:4318", sdk_disabled=True))
    )

    assert isinstance(container.command_tracer(), NoopCommandTracer)


def test_container_falls_back_to_the_noop_tracer_when_the_adapter_constructor_raises(
    container: Container, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from winter_cli.core.internal import otel_command_tracer
    from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
    from winter_cli.core.tracing import TracingSettings

    def explode(self: object, settings: TracingSettings) -> None:
        raise RuntimeError("adapter construction failed")

    monkeypatch.setattr(otel_command_tracer.OtelCommandTracer, "__init__", explode)
    container.tracing_settings.override(providers.Object(TracingSettings(otlp_endpoint="http://localhost:4318")))

    tracer = container.command_tracer()

    assert isinstance(tracer, NoopCommandTracer)
    tracer.start_command("winter ws init")
    tracer.end_command(None)
    assert caplog.records == []

    with caplog.at_level("DEBUG", logger="winter_cli.core.internal.unavailable_command_tracer"):
        tracer.export()

    assert [record.levelname for record in caplog.records] == ["DEBUG"]
    assert caplog.records[0].exc_info is not None
    assert "adapter construction failed" in str(caplog.records[0].exc_info[1])


# ── The git adapters open their spans on the process's one tracer ────────────


def _bind_fake_tracer(container: Container) -> FakeOperationTracer:
    tracer = FakeOperationTracer()
    container.command_tracer.override(providers.Object(tracer))
    return tracer


def test_container_binds_the_command_tracer_into_every_git_adapter(container: Container, tmp_path: Path) -> None:
    tracer = _bind_fake_tracer(container)
    workspace, project = init_project(tmp_path)
    add_env_worktree(project.main_path, tmp_path, "alpha", "main")
    env = FeatureEnvironment(workspace=workspace, name="alpha", index=1, path=tmp_path / "alpha")

    container.git_repo().get_local_branches(project.main_path, repo_name=project.name, env=None)
    container.repo_repo().get_project_status(project)
    container.worktree_repo().get_environment_status(env, [project])

    assert [span.name for span in tracer.spans] == ["git branch", "git status", "git config"]


def test_dashboard_snapshot_service_hands_its_tracer_to_the_workspace_repository_it_builds(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = _bind_fake_tracer(container)
    built: list[dict[str, object]] = []

    class _RecordingWorkspaceRepository(ReadWorkspaceRepository):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(dashboard_snapshot_service, "ReadWorkspaceRepository", _RecordingWorkspaceRepository)

    container.dashboard_snapshot_svc().collect_for_dashboard()

    assert [kwargs["tracer"] for kwargs in built] == [tracer]


def test_fresh_init_container_git_adapter_opens_spans_on_the_launching_tracer(tmp_path: Path) -> None:
    launching_tracer = FakeOperationTracer()
    _workspace, project = init_project(tmp_path)

    fresh = FreshInitServiceFactory(launching_tracer).build_container()  # type: ignore[arg-type]
    fresh.git_repo().get_local_branches(project.main_path, repo_name=project.name, env=None)

    assert [(span.name, span.attributes) for span in launching_tracer.spans] == [
        ("git branch", {"winter.repo": project.name})
    ]


# ── The service and provision span sites open their spans on the process's one tracer ──


def test_container_binds_the_command_tracer_into_the_service_and_provision_span_sites(container: Container) -> None:
    tracer = _bind_fake_tracer(container)

    assert container.service_fan_out_svc()._tracer is tracer
    assert container.service_readiness_svc()._tracer is tracer
    assert container.provision_execution_svc()._tracer is tracer


# ── The dashboard's screens open their session roots on the process's one tracer ─


def test_container_binds_the_command_tracer_into_every_screen_that_runs_thread_workers(container: Container) -> None:
    tracer = FakeSessionTracer()
    container.command_tracer.override(providers.Object(tracer))

    screens = [
        container.workspace_screen(),
        container.worktree_detail_screen(worktree_name="alpha"),
        container.standalone_detail_screen(repo_name="notes"),
        container.agent_matrix_screen(),
    ]

    assert [screen._session_tracer for screen in screens] == [tracer] * 4  # type: ignore[attr-defined]


# ── Provider resolution ──────────────────────────────────────────────────────


def test_every_provider_resolves_or_its_lazy_target_imports(container: Container) -> None:
    """Every provider on `Container` is covered: it resolves against the fixture workspace, or, when it
    needs call-time arguments, its `_lazy` target imports. A wrong `module:attr` path fails here."""
    unresolved: list[str] = []
    for name, provider in Container.providers.items():
        try:
            getattr(container, name)()
        except TypeError:
            provides = getattr(provider, "provides", None)
            if not isinstance(provides, LazyTarget):
                unresolved.append(name)
                continue
            provides.resolve()
    assert unresolved == []


def test_importing_the_container_imports_no_service_module() -> None:
    code = (
        "import sys, winter_cli.container\n"
        "print('\\n'.join(m for m in sorted(sys.modules) if m.startswith('winter_cli.modules')))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == []
