from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import click
from dependency_injector import containers, providers

from winter_cli.config.internal.cwd_workspace_locator import CwdWorkspaceLocator
from winter_cli.config.internal.write_winter_configuration_repository import (
    WriteWinterConfigurationRepository,
)
from winter_cli.config.workspace import WorkspaceConfigService
from winter_cli.core.internal.click_cli_input_validation_service import (
    ClickCliInputValidationService,
)
from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService
from winter_cli.core.internal.local_filesystem import LocalFilesystem
from winter_cli.core.internal.local_subprocess_runner import LocalSubprocessRunner
from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader

# NB: the doctor, lint, graph, and tui (textual) command trees are deliberately
# NOT imported at module top — see `_lazy` below. They are pulled in on first
# provider resolution so the hot `winter ws` path (which instantiates this
# container on every invocation) never pays for the textual / probe trees it
# doesn't touch.
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.extension_claudemd_service import ExtensionClaudemdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.fetch_reporter import JsonFetchReporter, StreamFetchReporter
from winter_cli.modules.workspace.handlers.destroy_handler import DestroyHandler
from winter_cli.modules.workspace.handlers.init_handler import InitHandler
from winter_cli.modules.workspace.handlers.repo_handler import RepoHandler
from winter_cli.modules.workspace.handlers.workspace_handler import WorkspaceHandler
from winter_cli.modules.workspace.init_reporter import JsonReporter, StreamReporter
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.config_lock_repository import WriteConfigLockRepository
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.gitpython_repository import GitPythonRepository
from winter_cli.modules.workspace.internal.read_workspace_repository import ReadWorkspaceRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.toml_env_index_registry import TomlEnvIndexRegistry
from winter_cli.modules.workspace.internal.write_repo_repository import WriteRepoRepository
from winter_cli.modules.workspace.merge_reporter import JsonMergeReporter, StreamMergeReporter
from winter_cli.modules.workspace.prune_service import PruneService
from winter_cli.modules.workspace.pull_reporter import JsonPullReporter, StreamPullReporter
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_merge_service import WorkspaceMergeService
from winter_cli.modules.workspace.workspace_push_service import WorkspacePushService
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService
from winter_cli.plugins.internal.importlib_plugin_loader import ImportlibPluginLoader
from winter_cli.plugins.loader import PluginRegistry


def _lazy(target: str) -> Callable[..., Any]:
    """Build a provider `provides` callable that imports its class on first use.

    `target` is a `"module:attr"` reference. The returned callable forwards
    `*args, **kwargs` (the provider's injected dependencies) to the resolved
    class, importing the module only the first time the provider is resolved.
    This keeps the doctor / lint / tui (textual) trees out of the module-load
    import graph, so building `Container()` on the hot `winter ws` path doesn't
    drag them in — they load only when their command (doctor / lint / dashboard)
    actually resolves a provider that needs them.
    """
    resolved: list[Callable[..., Any]] = []

    def make(*args: Any, **kwargs: Any) -> Any:
        if not resolved:
            module_name, attr = target.split(":", 1)
            resolved.append(getattr(importlib.import_module(module_name), attr))
        return resolved[0](*args, **kwargs)

    return make


class Container(containers.DeclarativeContainer):
    """DI container for the winter CLI."""

    cli_output_svc = providers.Singleton(ClickCliOutputService)
    cli_input_validation_svc = providers.Singleton(ClickCliInputValidationService)

    # Cross-cutting I/O seams. Adapters confine `pathlib`/`shutil`/`os`,
    # `tomllib`, and `subprocess` so service code depends on Protocols, not
    # the standard library. See core/{filesystem,config_file,subprocess_runner}.py.
    fs = providers.Singleton(LocalFilesystem)
    config_file_reader = providers.Singleton(TomllibConfigFileReader)
    subprocess_runner = providers.Singleton(LocalSubprocessRunner)

    # Workspace-root discovery seam — lets WorkspaceConfigService accept a
    # locator instead of reaching `Path.cwd()` directly. Tests substitute a
    # fake that returns a fixed path.
    workspace_locator = providers.Singleton(CwdWorkspaceLocator)

    workspace_config_svc = providers.Singleton(
        WorkspaceConfigService,
        workspace_locator=workspace_locator,
        fs=fs,
        config_file_reader=config_file_reader,
    )
    workspace_config = providers.Singleton(workspace_config_svc.provided.load.call())

    write_winter_config_repo = providers.Factory(
        WriteWinterConfigurationRepository,
        workspace_config=workspace_config,
        fs=fs,
    )

    config_lock_repo = providers.Factory(
        WriteConfigLockRepository,
        workspace_root=workspace_config.provided.workspace_root,
        fs=fs,
    )

    _env_index_registry_path = providers.Callable(
        lambda cfg: cfg.workspace_root / ".winter" / "state.toml",
        workspace_config,
    )

    env_index_registry = providers.Singleton(
        TomlEnvIndexRegistry,
        state_path=_env_index_registry_path,
        fs=fs,
    )

    # Factory for structured RepoError instances — injected into every class
    # that translates GitPython exceptions into winter's error type.
    repo_error_factory = providers.Singleton(RepoErrorFactory)

    # Service-level git seam used by InitService / DestroyService / PruneService
    # (not by IRead/IWriteRepoRepository, which already own domain-level git).
    # The adapter wraps `git.GitCommandError` into `RepoError` via repo_error_factory.
    git_repo = providers.Singleton(GitPythonRepository, error_factory=repo_error_factory)

    # Importlib-based plugin module loader. Confines `importlib.util` and
    # `sys.modules` mutation so the registry depends on a Protocol.
    plugin_loader = providers.Singleton(ImportlibPluginLoader)

    # Central git-ops chokepoint: owns the parallelism cap and retry policy
    # for network-touching git operations.
    git_ops_svc = providers.Singleton(GitOpsService, error_factory=repo_error_factory)

    repo_repo = providers.Factory(
        WriteRepoRepository,
        error_factory=repo_error_factory,
        git_ops=git_ops_svc,
    )
    workspace = providers.Singleton(
        repo_repo.provided.get_workspace.call(
            workspace_config.provided.workspace_root,
            workspace_config.provided.session_prefix,
            workspace_config.provided.main_branch,
            workspace_config.provided.base_port,
            workspace_config.provided.ports_per_env,
        ),
    )

    repo_factory = providers.Singleton(
        RepositoryFactory,
        config=workspace_config,
    )

    plugin_registry = providers.Singleton(
        PluginRegistry.load,
        workspace=workspace,
        fs=fs,
        config_file_reader=config_file_reader,
        plugin_loader=plugin_loader,
        standalone_repos=repo_factory.provided.get_standalone_repos.call(),
    )

    worktree_repo = providers.Factory(
        ReadWorkspaceRepository,
        error_factory=repo_error_factory,
        env_aliases=workspace_config.provided.env_aliases,
        envs_per_workspace=workspace_config.provided.envs_per_workspace,
        registry=env_index_registry,
    )

    drift_warning_svc = providers.Factory(
        DriftWarningService,
        workspace=workspace,
        repo_factory=repo_factory,
        fs=fs,
        click=providers.Object(click),
    )

    env_status_svc = providers.Factory(
        EnvStatusService,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
    )

    workspace_sync_svc = providers.Factory(
        WorkspaceSyncService,
        env_status_svc=env_status_svc,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        git_ops=git_ops_svc,
        git_repo=git_repo,
        config_lock_repo=config_lock_repo,
    )

    workspace_push_svc = providers.Factory(
        WorkspacePushService,
        env_status_svc=env_status_svc,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
    )

    workspace_merge_svc = providers.Factory(
        WorkspaceMergeService,
        env_status_svc=env_status_svc,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        git_ops=git_ops_svc,
    )

    env_checkout_svc = providers.Factory(
        EnvCheckoutService,
        repo_repo=repo_repo,
    )

    extension_manifest_loader = providers.Singleton(
        ExtensionManifestLoader,
        config_file_reader=config_file_reader,
    )

    extension_symlink_svc = providers.Singleton(
        ExtensionSymlinkService,
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    extension_hook_svc = providers.Singleton(
        ExtensionHookService,
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        manifest_loader=extension_manifest_loader,
        registry=env_index_registry,
    )

    extension_exclude_svc = providers.Singleton(
        ExtensionExcludeService,
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    extension_claudemd_svc = providers.Singleton(
        ExtensionClaudemdService,
        config=workspace_config,
        fs=fs,
    )

    prune_svc = providers.Factory(
        PruneService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_exclude_svc=extension_exclude_svc,
        fs=fs,
        git_repo=git_repo,
    )

    workspace_snapshot_svc = providers.Factory(
        WorkspaceSnapshotService,
        workspace=workspace,
        env_status_svc=env_status_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        drift_warning_svc=drift_warning_svc,
        prune_svc=prune_svc,
        config_lock_repo=config_lock_repo,
        git_repo=git_repo,
        dashboard_layout=workspace_config.provided.dashboard.layout,
    )

    init_svc = providers.Factory(
        InitService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_symlink_svc=extension_symlink_svc,
        extension_hook_svc=extension_hook_svc,
        extension_exclude_svc=extension_exclude_svc,
        extension_claudemd_svc=extension_claudemd_svc,
        fs=fs,
        subprocess_runner=subprocess_runner,
        git_repo=git_repo,
        git_ops=git_ops_svc,
        registry=env_index_registry,
        config_lock_repo=config_lock_repo,
    )

    stream_reporter = providers.Factory(
        StreamReporter,
        click=providers.Object(click),
    )

    json_reporter = providers.Factory(
        JsonReporter,
        click=providers.Object(click),
    )

    stream_fetch_reporter = providers.Factory(
        StreamFetchReporter,
        click=providers.Object(click),
    )

    json_fetch_reporter = providers.Factory(
        JsonFetchReporter,
        click=providers.Object(click),
    )

    stream_pull_reporter = providers.Factory(
        StreamPullReporter,
        click=providers.Object(click),
    )

    json_pull_reporter = providers.Factory(
        JsonPullReporter,
        click=providers.Object(click),
    )

    stream_merge_reporter = providers.Factory(
        StreamMergeReporter,
        click=providers.Object(click),
    )

    json_merge_reporter = providers.Factory(
        JsonMergeReporter,
        click=providers.Object(click),
    )

    reporter_factory = providers.Singleton(
        ReporterFactory,
        stream_init_reporter=stream_reporter.provider,
        json_init_reporter=json_reporter.provider,
        stream_fetch_reporter=stream_fetch_reporter.provider,
        json_fetch_reporter=json_fetch_reporter.provider,
        stream_pull_reporter=stream_pull_reporter.provider,
        json_pull_reporter=json_pull_reporter.provider,
        stream_merge_reporter=stream_merge_reporter.provider,
        json_merge_reporter=json_merge_reporter.provider,
    )

    workspace_handler = providers.Factory(
        WorkspaceHandler,
        env_status_svc=env_status_svc,
        workspace_sync_svc=workspace_sync_svc,
        workspace_push_svc=workspace_push_svc,
        workspace_merge_svc=workspace_merge_svc,
        env_checkout_svc=env_checkout_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        drift_warning_svc=drift_warning_svc,
        prune_svc=prune_svc,
        reporter_factory=reporter_factory,
        cli_output_svc=cli_output_svc,
        workspace=workspace,
        workspace_snapshot_svc=workspace_snapshot_svc,
        env_aliases=workspace_config.provided.env_aliases,
        envs_per_workspace=workspace_config.provided.envs_per_workspace,
        env_index_registry=env_index_registry,
        plugin_registry=plugin_registry,
    )

    repo_handler = providers.Factory(
        RepoHandler,
        repo_factory=repo_factory,
        drift_warning_svc=drift_warning_svc,
        cli_output_svc=cli_output_svc,
        cli_input_validation_svc=cli_input_validation_svc,
        write_winter_config_repo=write_winter_config_repo,
        workspace=workspace,
    )

    init_handler = providers.Factory(
        InitHandler,
        init_service=init_svc,
        reporter_factory=reporter_factory,
    )

    # ── capability spec loader: machine-readable contracts (cold path) ──────
    # Declared here (before capability_registry_svc) so the registry can
    # reference it. The service is cold-path only (capabilities / ext verify).

    spec_loader = providers.Singleton(
        _lazy("winter_cli.modules.capability.spec_loader:SpecLoader"),
        config_file_reader=config_file_reader,
    )

    capability_registry_svc = providers.Factory(
        _lazy("winter_cli.modules.capability.capability_registry_service:CapabilityRegistryService"),
        repo_factory=repo_factory,
        manifest_loader=extension_manifest_loader,
        bindings=workspace_config.provided.capabilities,
        fs=fs,
        spec_loader=spec_loader,
    )

    core_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.core_probe_service:CoreProbeService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        config_file_reader=config_file_reader,
        repo_factory=repo_factory,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
    )

    workspace_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.workspace_probe_service:WorkspaceProbeService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
    )

    extension_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.extension_probe_service:ExtensionProbeService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        manifest_loader=extension_manifest_loader,
    )

    capability_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.capability_probe_service:CapabilityProbeService"),
        registry=capability_registry_svc,
    )

    port_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.port_probe_service:PortProbeService"),
        config=workspace_config,
        fs=fs,
        registry=env_index_registry,
    )

    provision_manifest_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.provision.manifest_probe_service:ProvisionManifestProbeService"),
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
        config_file_reader=config_file_reader,
    )

    skill_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.skill_probe_service:SkillProbeService"),
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    doctor_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.doctor_service:DoctorService"),
        core_probe_svc=core_probe_svc,
        workspace_probe_svc=workspace_probe_svc,
        extension_probe_svc=extension_probe_svc,
        repo_factory=repo_factory,
        capability_probe_svc=capability_probe_svc,
        port_probe_svc=port_probe_svc,
        provision_manifest_probe_svc=provision_manifest_probe_svc,
        skill_probe_svc=skill_probe_svc,
    )

    stream_doctor_reporter = providers.Factory(
        _lazy("winter_cli.modules.doctor.doctor_reporter:StreamDoctorReporter"),
        click=providers.Object(click),
    )

    json_doctor_reporter = providers.Factory(
        _lazy("winter_cli.modules.doctor.doctor_reporter:JsonDoctorReporter"),
        click=providers.Object(click),
    )

    doctor_handler = providers.Factory(
        _lazy("winter_cli.modules.doctor.handler:DoctorHandler"),
        doctor_service=doctor_svc,
        stream_reporter=stream_doctor_reporter,
        json_reporter=json_doctor_reporter,
    )

    # ── graph: module dependency graph from winter-ext.toml `requires` ──────

    graph_svc = providers.Factory(
        _lazy("winter_cli.modules.graph.graph_service:GraphService"),
        fs=fs,
        manifest_loader=extension_manifest_loader,
        repo_factory=repo_factory,
    )

    stream_graph_reporter = providers.Factory(
        _lazy("winter_cli.modules.graph.graph_reporter:StreamGraphReporter"),
        click=providers.Object(click),
    )

    json_graph_reporter = providers.Factory(
        _lazy("winter_cli.modules.graph.graph_reporter:JsonGraphReporter"),
        click=providers.Object(click),
    )

    graph_handler = providers.Factory(
        _lazy("winter_cli.modules.graph.handler:GraphHandler"),
        graph_service=graph_svc,
        stream_reporter=stream_graph_reporter,
        json_reporter=json_graph_reporter,
    )

    # ── service: env-file sourcer (shell-sources per-scope .winter.env files) ─

    env_file_sourcer = providers.Singleton(
        _lazy("winter_cli.modules.service.internal.shell_env_file_sourcer:ShellEnvFileSourcer")
    )

    # ── service: dispatch to the registered orchestrator extension ──────────

    # Holds the effective `--service-orchestrator` / WINTER_SERVICE_ORCHESTRATOR
    # override for this invocation. Defaults to None (use config value). The CLI
    # boundary overwrites this via `container.service_orchestrator_override.override()`
    # when a non-None override is present, before resolving `service_handler`.
    service_orchestrator_override = providers.Object(None)

    # ── capabilities: read-only slot introspection ───────────────────────────

    stream_capability_reporter = providers.Factory(
        _lazy("winter_cli.modules.capability.capability_reporter:StreamCapabilityReporter"),
        click=providers.Object(click),
    )

    json_capability_reporter = providers.Factory(
        _lazy("winter_cli.modules.capability.capability_reporter:JsonCapabilityReporter"),
        click=providers.Object(click),
    )

    capabilities_handler = providers.Factory(
        _lazy("winter_cli.modules.capability.handler:CapabilitiesHandler"),
        registry=capability_registry_svc,
        stream_reporter=stream_capability_reporter,
        json_reporter=json_capability_reporter,
    )

    service_orchestrator_resolver = providers.Factory(
        _lazy("winter_cli.modules.service.orchestrator_resolver:ServiceOrchestratorResolver"),
        registry=capability_registry_svc,
        repo_factory=repo_factory,
        manifest_loader=extension_manifest_loader,
        fs=fs,
        override=service_orchestrator_override,
        workspace_root=workspace_config.provided.workspace_root,
    )

    status_document_parser = providers.Singleton(_lazy("winter_cli.modules.service.status_parser:StatusDocumentParser"))

    service_describe_parser = providers.Singleton(
        _lazy("winter_cli.modules.service.describe_parser:DescribeResultParser")
    )

    service_describe_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_provider_index:ServiceDescribeService"),
        subprocess_runner=subprocess_runner,
        describe_parser=service_describe_parser,
        workspace_root=workspace_config.provided.workspace_root,
    )

    service_manifest_collector_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_manifest_collector:ServiceManifestCollectorService"),
        workspace_root=workspace_config.provided.workspace_root,
        workspace_service_defs_raw=workspace_config.provided.service_defs_raw,
        manifest_loader=extension_manifest_loader,
        repo_factory=repo_factory,
        fs=fs,
    )

    service_catalog_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_catalog_service:ServiceCatalogService"),
        subprocess_runner=subprocess_runner,
        workspace_root=workspace_config.provided.workspace_root,
    )

    service_fan_out_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_fan_out_service:ServiceFanOutService"),
        subprocess_runner=subprocess_runner,
        workspace_root=workspace_config.provided.workspace_root,
        manifest_collector=service_manifest_collector_svc,
    )

    stream_service_reporter = providers.Factory(
        _lazy("winter_cli.modules.service.service_reporter:StreamServiceReporter"),
        click=providers.Object(click),
        cli_output=cli_output_svc,
    )

    json_service_reporter = providers.Factory(
        _lazy("winter_cli.modules.service.service_reporter:JsonServiceReporter"),
        click=providers.Object(click),
        cli_output=cli_output_svc,
    )

    service_dispatch_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_dispatch_service:ServiceDispatchService"),
        subprocess_runner=subprocess_runner,
        orchestrator_resolver=service_orchestrator_resolver,
        fan_out_service=service_fan_out_svc,
        describe_service=service_describe_svc,
        workspace_root=workspace_config.provided.workspace_root,
        reporter=stream_service_reporter,
    )

    service_logs_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_logs_service:ServiceLogsService"),
        subprocess_runner=subprocess_runner,
        orchestrator_resolver=service_orchestrator_resolver,
        describe_service=service_describe_svc,
        workspace_root=workspace_config.provided.workspace_root,
    )

    service_status_matrix_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_status_matrix_service:ServiceStatusMatrixService"),
        subprocess_runner=subprocess_runner,
        describe_service=service_describe_svc,
        env_file_sourcer=env_file_sourcer,
        status_parser=status_document_parser,
        workspace_config=workspace_config,
        env_index_registry=env_index_registry,
        workspace_root=workspace_config.provided.workspace_root,
    )

    service_status_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_status_service:ServiceStatusService"),
        orchestrator_resolver=service_orchestrator_resolver,
        status_parser=status_document_parser,
        matrix_service=service_status_matrix_svc,
    )

    service_readiness_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_readiness_service:ServiceReadinessService"),
        status_service=service_status_svc,
    )

    service_handler = providers.Factory(
        _lazy("winter_cli.modules.service.handler:ServiceHandler"),
        dispatch_service=service_dispatch_svc,
        logs_service=service_logs_svc,
        status_service=service_status_svc,
        readiness_service=service_readiness_svc,
        stream_reporter=stream_service_reporter,
        json_reporter=json_service_reporter,
    )

    # ── provision: re-runnable env readiness (dependency/resource/data) ────

    provision_execution_svc = providers.Factory(
        _lazy("winter_cli.modules.provision.execution_service:ProvisionExecutionService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        manifest_loader=extension_manifest_loader,
        repo_factory=repo_factory,
        registry=env_index_registry,
    )

    provision_service_check = providers.Factory(
        _lazy("winter_cli.modules.provision.service_check_service:ProvisionServiceCheck"),
        status_svc=service_status_svc,
        dispatch_svc=service_dispatch_svc,
    )

    provision_svc = providers.Factory(
        _lazy("winter_cli.modules.provision.provision_service:ProvisionService"),
        config=workspace_config,
        execution_svc=provision_execution_svc,
        manifest_loader=extension_manifest_loader,
        repo_factory=repo_factory,
        service_check=provision_service_check,
        fs=fs,
    )

    stream_provision_reporter = providers.Factory(
        _lazy("winter_cli.modules.provision.provision_reporter:StreamProvisionReporter"),
        click=providers.Object(click),
    )

    json_provision_reporter = providers.Factory(
        _lazy("winter_cli.modules.provision.provision_reporter:JsonProvisionReporter"),
        click=providers.Object(click),
    )

    provision_command_handler = providers.Factory(
        _lazy("winter_cli.modules.provision.handler:ProvisionCommandHandler"),
        provision_service=provision_svc,
        stream_reporter=stream_provision_reporter,
        json_reporter=json_provision_reporter,
    )

    destroy_svc = providers.Factory(
        DestroyService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_hook_svc=extension_hook_svc,
        fs=fs,
        git_repo=git_repo,
        registry=env_index_registry,
        provision_svc=provision_svc,
    )

    destroy_handler = providers.Factory(
        DestroyHandler,
        destroy_service=destroy_svc,
        reporter_factory=reporter_factory,
    )

    # ── lint: dispatcher to extension-contributed convention checks ─────────

    # Path to the winter CLI that launched this run, handed to every lint
    # script as WINTER_CLI so checks can call back (e.g. `$WINTER_CLI graph`).
    winter_cli_path = providers.Callable(_lazy("winter_cli.modules.lint.scope_env:resolve_winter_cli_path"))

    # Absolute path to the bundled extractability check, resolved relative to the
    # winter-cli source tree (its sibling tools/winter-lint/ directory).
    extractability_script_path = providers.Callable(
        _lazy("winter_cli.modules.lint.core_lint_service:default_extractability_script_path")
    )

    core_lint_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.core_lint_service:CoreLintService"),
        workspace_root=workspace_config.provided.workspace_root,
        fs=fs,
        subprocess_runner=subprocess_runner,
        winter_cli_path=winter_cli_path,
        script_path=extractability_script_path,
        file_size_config=workspace_config.provided.file_size_lint,
        orchestrator_resolver=service_orchestrator_resolver,
        catalog_service=service_catalog_svc,
    )

    workspace_lint_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.workspace_lint_service:WorkspaceLintService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        winter_cli_path=winter_cli_path,
    )

    extension_lint_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.extension_lint_service:ExtensionLintService"),
        config=workspace_config,
        fs=fs,
        subprocess_runner=subprocess_runner,
        manifest_loader=extension_manifest_loader,
        winter_cli_path=winter_cli_path,
    )

    lint_scope_resolver = providers.Factory(
        _lazy("winter_cli.modules.lint.scope_resolver:LintScopeResolver"),
        config=workspace_config,
        repo_factory=repo_factory,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
        subprocess_runner=subprocess_runner,
    )

    lint_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.lint_service:LintService"),
        core_lint_svc=core_lint_svc,
        workspace_lint_svc=workspace_lint_svc,
        extension_lint_svc=extension_lint_svc,
        repo_factory=repo_factory,
    )

    stream_lint_reporter = providers.Factory(
        _lazy("winter_cli.modules.lint.lint_reporter:StreamLintReporter"),
        click=providers.Object(click),
    )

    json_lint_reporter = providers.Factory(
        _lazy("winter_cli.modules.lint.lint_reporter:JsonLintReporter"),
        click=providers.Object(click),
    )

    lint_handler = providers.Factory(
        _lazy("winter_cli.modules.lint.handler:LintHandler"),
        lint_service=lint_svc,
        scope_resolver=lint_scope_resolver,
        stream_reporter=stream_lint_reporter,
        json_reporter=json_lint_reporter,
    )

    # ── ext: extension verification (cold path) ─────────────────────────────

    ext_verify_svc = providers.Factory(
        _lazy("winter_cli.modules.ext.verify_service:ConformanceVerifyService"),
        subprocess_runner=subprocess_runner,
        orchestrator_resolver=service_orchestrator_resolver,
        spec_loader=spec_loader,
        workspace_root=workspace_config.provided.workspace_root,
    )

    stream_verify_reporter = providers.Factory(
        _lazy("winter_cli.modules.ext.verify_reporter:StreamVerifyReporter"),
        click=providers.Object(click),
    )

    json_verify_reporter = providers.Factory(
        _lazy("winter_cli.modules.ext.verify_reporter:JsonVerifyReporter"),
        click=providers.Object(click),
    )

    ext_verify_handler = providers.Factory(
        _lazy("winter_cli.modules.ext.handler:ExtVerifyHandler"),
        verify_service=ext_verify_svc,
        stream_reporter=stream_verify_reporter,
        json_reporter=json_verify_reporter,
    )

    ext_scaffold_svc = providers.Factory(
        _lazy("winter_cli.modules.ext.scaffold_service:ExtScaffoldService"),
        spec_loader=spec_loader,
        fs=fs,
    )

    ext_new_handler = providers.Factory(
        _lazy("winter_cli.modules.ext.handler:ExtNewHandler"),
        scaffold_service=ext_scaffold_svc,
        click=providers.Object(click),
    )

    # Session-scoped log buffer for RepoErrors captured during dashboard
    # polling and actions. Singleton so navigating between screens preserves
    # the entries within a single dashboard session.
    error_log_svc = providers.Singleton(_lazy("winter_cli.modules.tui.error_log:ErrorLogService"))

    # Resolves `[keybindings]` overrides onto each screen's action defaults and
    # owns the chord-sequence timeout. Singleton — config is immutable per run.
    keybinding_resolver = providers.Singleton(
        _lazy("winter_cli.modules.tui.keybindings:KeybindingResolver"),
        config=workspace_config.provided.keybindings,
    )

    workspace_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.workspace:WorkspaceScreen"),
        snapshot_svc=workspace_snapshot_svc,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
        dashboard_layout=workspace_config.provided.dashboard.layout,
    )

    worktree_detail_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.worktree_detail:WorktreeDetailScreen"),
        env_status_svc=env_status_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
    )

    standalone_detail_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.standalone_detail:StandaloneDetailScreen"),
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
    )

    error_log_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.error_log:ErrorLogScreen"),
        error_log=error_log_svc,
    )
