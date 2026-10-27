from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

import click
from dependency_injector import containers, providers

from winter_cli.config.internal.cwd_workspace_locator import CwdWorkspaceLocator
from winter_cli.config.internal.tomlkit_local_overlay_repository import TomlkitLocalOverlayRepository
from winter_cli.config.internal.write_winter_configuration_repository import (
    WriteWinterConfigurationRepository,
)
from winter_cli.config.workspace import WorkspaceConfigService
from winter_cli.core.internal.click_cli_input_validation_service import (
    ClickCliInputValidationService,
)
from winter_cli.core.internal.click_cli_output_service import ClickCliOutputService
from winter_cli.core.internal.local_filesystem import LocalFilesystem
from winter_cli.core.internal.local_subprocess_runner import NON_INTERACTIVE_ENV, LocalSubprocessRunner
from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader
from winter_cli.core.internal.unavailable_command_tracer import UnavailableCommandTracer
from winter_cli.core.tracing import ICommandTracer, TracingSettings
from winter_cli.modules.workspace.agent_install import ExtensionAgentService
from winter_cli.modules.workspace.agent_transform.agent_copy_inspector import AgentCopyInspector
from winter_cli.modules.workspace.agent_transform.agent_enumerator import CanonicalAgentEnumerator
from winter_cli.modules.workspace.dashboard_snapshot_service import DashboardSnapshotService

# NB: the doctor, lint, graph, and tui (textual) command trees are deliberately
# NOT imported at module top — see `_lazy` below. They are pulled in on first
# provider resolution so the hot `winter ws` path (which instantiates this
# container on every invocation) never pays for the textual / probe trees it
# doesn't touch.
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.env_checkout_service import EnvCheckoutService
from winter_cli.modules.workspace.env_clean_service import EnvCleanService
from winter_cli.modules.workspace.env_index import EnvPortBaseResolver
from winter_cli.modules.workspace.env_reset_service import EnvResetService
from winter_cli.modules.workspace.env_restack_plan_service import EnvRestackPlanService
from winter_cli.modules.workspace.env_restack_service import EnvRestackService
from winter_cli.modules.workspace.env_status_service import EnvStatusService
from winter_cli.modules.workspace.env_target import EnvNameDiscovery, EnvTargetService
from winter_cli.modules.workspace.extension_agentsmd_service import ExtensionAgentsMdService
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.extension_symlink_service import ExtensionSymlinkService
from winter_cli.modules.workspace.fetch_reporter import JsonFetchReporter, StreamFetchReporter
from winter_cli.modules.workspace.handlers.destroy_handler import DestroyHandler
from winter_cli.modules.workspace.handlers.fingerprint_handler import FingerprintHandler
from winter_cli.modules.workspace.handlers.init_handler import InitHandler
from winter_cli.modules.workspace.handlers.repo_handler import RepoHandler
from winter_cli.modules.workspace.handlers.restack_handler import RestackHandler
from winter_cli.modules.workspace.handlers.workspace_handler import WorkspaceHandler
from winter_cli.modules.workspace.init_reporter import JsonReporter, StreamReporter
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.config_lock_repository import WriteConfigLockRepository
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.gitpython_repository import GitPythonRepository
from winter_cli.modules.workspace.internal.gitpython_workspace_exclude_locator import GitPythonWorkspaceExcludeLocator
from winter_cli.modules.workspace.internal.read_workspace_repository import ReadWorkspaceRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.subprocess_command_entry_runner import SubprocessCommandEntryRunner
from winter_cli.modules.workspace.internal.subprocess_nested_workspace_runner import SubprocessNestedWorkspaceRunner
from winter_cli.modules.workspace.internal.toml_env_index_registry import TomlEnvIndexRegistry
from winter_cli.modules.workspace.internal.write_repo_repository import WriteRepoRepository
from winter_cli.modules.workspace.merge_reporter import JsonMergeReporter, StreamMergeReporter
from winter_cli.modules.workspace.nested_workspace_service import NestedWorkspaceService
from winter_cli.modules.workspace.prune_service import PruneService
from winter_cli.modules.workspace.pull_reporter import JsonPullReporter, StreamPullReporter
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_fingerprint_service import WorkspaceFingerprintService
from winter_cli.modules.workspace.workspace_merge_service import WorkspaceMergeService
from winter_cli.modules.workspace.workspace_push_service import WorkspacePushService
from winter_cli.modules.workspace.workspace_skill_service import WorkspaceSkillService
from winter_cli.modules.workspace.workspace_snapshot_service import WorkspaceSnapshotService
from winter_cli.modules.workspace.workspace_sync_service import WorkspaceSyncService
from winter_cli.modules.workspace.worktree_safety import WorktreeSafetyService
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


def _select_command_tracer(settings: TracingSettings) -> ICommandTracer:
    """The process's one tracer: the OpenTelemetry adapter when tracing is on, else the no-op.

    The adapter is reached through `_lazy`, so a process with tracing off never imports
    `opentelemetry`. Tracing never changes a command's outcome, so any failure to import the
    SDK or construct the adapter (a malformed generic SDK variable raises at import) falls
    back to the no-op; the failure surfaces only at debug level.
    """
    if not settings.enabled:
        return NoopCommandTracer()
    try:
        return _lazy("winter_cli.core.internal.otel_command_tracer:OtelCommandTracer")(settings)
    except Exception as exc:
        return UnavailableCommandTracer(exc)


class FreshInitServiceFactory:
    """Builds an `InitService` from a fresh, non-interactive `Container` — the dashboard's `ws init`.

    The dashboard resolves its container once at launch, so its own `init_svc`
    would carry the launch-time `WorkspaceConfig` — an init run after a config
    edit would re-render agent copies from the stale config. Each call therefore
    builds a fresh `Container`, exactly as a `winter ws init` invocation does,
    against the same running build.

    No one can answer a prompt from inside the dashboard, so that container runs
    every child process non-interactively: the subprocess runner gives children
    `/dev/null` stdin and `GIT_TERMINAL_PROMPT=0`, and a clone gets the same git
    switch. git's own HTTPS credential prompt is then disabled, and a child that
    reads stdin (`mise trust`, a hook) sees EOF. A prompt that opens the terminal
    directly — an ssh passphrase or host-key prompt — is not covered and can still
    stall the run.

    A process has exactly one tracer, so every fresh container binds the launching
    container's tracer instance instead of making its own selection.
    """

    def __init__(self, command_tracer: ICommandTracer) -> None:
        self._command_tracer = command_tracer

    def build_container(self) -> Container:
        container = Container()
        container.command_tracer.override(providers.Object(self._command_tracer))
        container.subprocess_runner.override(
            providers.Singleton(LocalSubprocessRunner, trace_propagator=self._command_tracer, non_interactive=True)
        )
        container.git_repo.override(
            providers.Singleton(
                GitPythonRepository,
                error_factory=container.repo_error_factory,
                tracer=self._command_tracer,
                clone_env=NON_INTERACTIVE_ENV,
            )
        )
        return container

    def __call__(self) -> InitService:
        return self.build_container().init_svc()


class Container(containers.DeclarativeContainer):
    """DI container for the winter CLI."""

    cli_output_svc = providers.Singleton(ClickCliOutputService)
    cli_input_validation_svc = providers.Singleton(ClickCliInputValidationService)

    # Cross-cutting I/O seams. Adapters confine `pathlib`/`shutil`/`os`,
    # `tomllib`, and `subprocess` so service code depends on Protocols, not
    # the standard library. See core/{filesystem,config_file,subprocess_runner}.py.
    fs = providers.Singleton(LocalFilesystem)
    config_file_reader = providers.Singleton(TomllibConfigFileReader)

    # Tracing seam. `tracing_settings` is a value slot: the CLI boundary overwrites it
    # with `container.tracing_settings.override()` once it has read the launch-time
    # environment. `command_tracer` is the single binding point for the process's one
    # tracer, selected from the settings; every Container built after the boundary's binds
    # that same instance.
    tracing_settings = providers.Object(TracingSettings())
    command_tracer = providers.Singleton(_select_command_tracer, settings=tracing_settings)

    # Every child process the runner starts gets the active trace context through
    # the tracer (`ITracePropagator`).
    subprocess_runner = providers.Singleton(LocalSubprocessRunner, trace_propagator=command_tracer)

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
    git_repo = providers.Singleton(GitPythonRepository, error_factory=repo_error_factory, tracer=command_tracer)

    # The one resolver for the git exclude file holding the workspace root's managed
    # blocks (`.git/info/exclude`, or the worktree's own git-dir file for a linked
    # worktree root). Every workspace-exclude reader and writer takes it.
    workspace_exclude_locator = providers.Singleton(
        GitPythonWorkspaceExcludeLocator,
        workspace_root=workspace_config.provided.workspace_root,
        error_factory=repo_error_factory,
        tracer=command_tracer,
    )

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
        tracer=command_tracer,
    )
    # `DashboardSnapshotService._build` rebuilds an equivalent Workspace per
    # poll from its own reloaded config — a constructor change here must be
    # mirrored there too.
    workspace = providers.Singleton(
        repo_repo.provided.get_workspace.call(
            workspace_config.provided.workspace_root,
            workspace_config.provided.service_prefix,
            workspace_config.provided.main_branch,
            workspace_config.provided.base_port,
            workspace_config.provided.ports_per_env,
        ),
    )

    # `DashboardSnapshotService._build` (per dashboard poll) builds its own
    # RepositoryFactory from its reloaded config — a constructor change here
    # must be mirrored there. `AgentMatrixService` gets `repo_factory_for`
    # below instead, so its wiring lives only here.
    repo_factory = providers.Singleton(
        RepositoryFactory,
        config=workspace_config,
        fs=fs,
    )

    plugin_registry = providers.Singleton(
        PluginRegistry.load,
        workspace=workspace,
        fs=fs,
        config_file_reader=config_file_reader,
        plugin_loader=plugin_loader,
        # Deliberately standalone-only, not get_extension_repos(): this is
        # dashboard-TUI plugin.py discovery, not the skills/agents/doctor/lint/
        # graph extension surface winter#160 moved to project-repo extensions.
        # Revisit if a project-repo extension ever needs to ship a dashboard plugin.
        # A standalone that opted out with `extension = false` is data, so it ships none.
        standalone_repos=repo_factory.provided.get_extension_standalone_repos.call(),
    )

    # `DashboardSnapshotService._build` rebuilds its own ReadWorkspaceRepository
    # per poll from its own reloaded config — a constructor change here must be
    # mirrored there too.
    worktree_repo = providers.Factory(
        ReadWorkspaceRepository,
        error_factory=repo_error_factory,
        tracer=command_tracer,
        env_aliases=workspace_config.provided.env_aliases,
        envs_per_workspace=workspace_config.provided.envs_per_workspace,
        registry=env_index_registry,
    )

    # The env-target declaration on each command positional resolves this service while click
    # parses the invoked command. Discovery is handed over as a provider, so nothing about the
    # workspace is resolved until tracing is on and a target needs discovery: a glob, or any
    # name under `discovered_only` (lint), which runs discovery for literal names too.
    env_name_discovery = providers.Factory(
        EnvNameDiscovery,
        workspace_repo=worktree_repo,
        repo_factory=repo_factory,
        workspace=workspace,
    )

    env_target_service = providers.Factory(
        EnvTargetService,
        annotator=command_tracer,
        tracing_enabled=tracing_settings.provided.enabled,
        discovery_factory=env_name_discovery.provider,
    )

    drift_warning_svc = providers.Factory(
        DriftWarningService,
        workspace=workspace,
        repo_factory=repo_factory,
        fs=fs,
        click=providers.Object(click),
    )

    # `DashboardSnapshotService._build` rebuilds its own EnvStatusService per
    # poll from its own reloaded config — a constructor change here must be
    # mirrored there too.
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
        write_config_repo=write_winter_config_repo,
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

    worktree_safety_svc = providers.Factory(
        WorktreeSafetyService,
        repo_repo=repo_repo,
    )

    env_checkout_svc = providers.Factory(
        EnvCheckoutService,
        repo_repo=repo_repo,
        worktree_safety_svc=worktree_safety_svc,
    )

    env_reset_svc = providers.Factory(
        EnvResetService,
        repo_repo=repo_repo,
        worktree_safety_svc=worktree_safety_svc,
    )

    env_clean_svc = providers.Factory(
        EnvCleanService,
        repo_repo=repo_repo,
    )

    env_restack_plan_svc = providers.Factory(
        EnvRestackPlanService,
        repo_repo=repo_repo,
    )

    env_restack_svc = providers.Factory(
        EnvRestackService,
        repo_repo=repo_repo,
    )

    extension_manifest_loader = providers.Singleton(
        ExtensionManifestLoader,
        config_file_reader=config_file_reader,
    )

    canonical_agent_enumerator = providers.Singleton(
        CanonicalAgentEnumerator,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    agent_copy_inspector = providers.Singleton(
        AgentCopyInspector,
        fs=fs,
        manifest_loader=extension_manifest_loader,
        agent_enumerator=canonical_agent_enumerator,
    )

    extension_symlink_svc = providers.Singleton(
        ExtensionSymlinkService,
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    extension_agent_svc = providers.Singleton(
        ExtensionAgentService,
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
        agent_enumerator=canonical_agent_enumerator,
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
        exclude_locator=workspace_exclude_locator,
    )

    extension_agentsmd_svc = providers.Singleton(
        ExtensionAgentsMdService,
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
    )

    workspace_skill_svc = providers.Singleton(
        WorkspaceSkillService,
        config=workspace_config,
        fs=fs,
        exclude_locator=workspace_exclude_locator,
    )

    prune_svc = providers.Factory(
        PruneService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_exclude_svc=extension_exclude_svc,
        fs=fs,
        git_repo=git_repo,
        exclude_locator=workspace_exclude_locator,
    )

    # Reads and writes another workspace root's config files — how a nested
    # workspace receives the port band and prefix its outer env delegates.
    local_overlay_repo = providers.Singleton(
        TomlkitLocalOverlayRepository,
        fs=fs,
        config_file_reader=config_file_reader,
    )

    # Runs a `nested = true` repo's own winter CLI inside each env's worktree of
    # it — `winter` from PATH, so the shim resolves the nested root's CLI.
    nested_workspace_runner = providers.Singleton(
        SubprocessNestedWorkspaceRunner,
        subprocess_runner=subprocess_runner,
        error_factory=repo_error_factory,
    )
    outer_env_port_bases = providers.Factory(
        EnvPortBaseResolver,
        config=workspace_config,
        registry=env_index_registry,
    )
    # One per process: it verifies each nested root once and reuses that for
    # every later call into it, across init, status, and destroy.
    nested_workspace_svc = providers.Singleton(
        NestedWorkspaceService,
        runner=nested_workspace_runner,
        fs=fs,
        workspace_root=workspace_config.provided.workspace_root,
        environ=providers.Object(os.environ),
        port_bases=outer_env_port_bases,
        service_prefix=workspace_config.provided.service_prefix,
        ports_per_env=workspace_config.provided.ports_per_env,
        overlay_repo=local_overlay_repo,
        nested_repo_names=providers.Callable(
            lambda cfg: [repo.name for repo in cfg.project_repos if repo.nested], workspace_config
        ),
    )

    # `DashboardSnapshotService._build` rebuilds this same graph (workspace,
    # env_status_svc, worktree_repo, repo_factory, plus the non-config-derived
    # collaborators below) per poll from its own reloaded config — a
    # constructor change here must be mirrored there too.
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
        nested_svc=nested_workspace_svc,
    )

    # Dashboard-only: re-reads config.toml on every poll and rebuilds the
    # config-derived collaborators, so the dashboard's 30s refresh (which
    # resolves the container only once, at launch) surfaces repos/envs/
    # standalones added after launch. Singleton so it accumulates the
    # last-good config across polls (see DashboardSnapshotService).
    dashboard_snapshot_svc = providers.Singleton(
        DashboardSnapshotService,
        workspace_config_svc=workspace_config_svc,
        repo_error_factory=repo_error_factory,
        repo_repo=repo_repo,
        env_index_registry=env_index_registry,
        drift_warning_svc=drift_warning_svc,
        prune_svc=prune_svc,
        config_lock_repo=config_lock_repo,
        git_repo=git_repo,
        tracer=command_tracer,
    )

    init_svc = providers.Factory(
        InitService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_symlink_svc=extension_symlink_svc,
        extension_agent_svc=extension_agent_svc,
        extension_hook_svc=extension_hook_svc,
        extension_exclude_svc=extension_exclude_svc,
        extension_agentsmd_svc=extension_agentsmd_svc,
        fs=fs,
        subprocess_runner=subprocess_runner,
        git_repo=git_repo,
        git_ops=git_ops_svc,
        config_lock_repo=config_lock_repo,
        workspace_skill_svc=workspace_skill_svc,
        registry=env_index_registry,
        exclude_locator=workspace_exclude_locator,
        nested_svc=nested_workspace_svc,
    )

    # Command-band execution seam — the only new `subprocess` import site for
    # command-valued env-band entries. Fixed cwd at the workspace root.
    command_entry_runner = providers.Singleton(
        SubprocessCommandEntryRunner,
        workspace_root=workspace_config.provided.workspace_root,
        error_factory=repo_error_factory,
    )

    env_band_resolver = providers.Factory(
        _lazy("winter_cli.modules.workspace.env_band_resolver_service:EnvBandResolverService"),
        runner=command_entry_runner,
    )

    env_provisioner = providers.Factory(
        _lazy("winter_cli.modules.workspace.env_provisioner:EnvProvisionerService"),
        config=workspace_config,
        registry=env_index_registry,
        band_resolver=env_band_resolver,
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
        env_reset_svc=env_reset_svc,
        env_clean_svc=env_clean_svc,
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

    workspace_fingerprint_svc = providers.Factory(
        WorkspaceFingerprintService,
        repo_factory=repo_factory,
        git_repo=git_repo,
    )

    fingerprint_handler = providers.Factory(
        FingerprintHandler,
        fingerprint_svc=workspace_fingerprint_svc,
    )

    restack_handler = providers.Factory(
        RestackHandler,
        plan_svc=env_restack_plan_svc,
        execute_svc=env_restack_svc,
        repo_factory=repo_factory,
        workspace=workspace,
        cli_output_svc=cli_output_svc,
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

    env_discovery_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.env_discovery_service:EnvDiscoveryService"),
        fs=fs,
    )

    port_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.port_probe_service:PortProbeService"),
        config=workspace_config,
        fs=fs,
        registry=env_index_registry,
        env_discovery=env_discovery_svc,
    )

    env_bands_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.env_bands_probe_service:EnvBandsProbeService"),
        config=workspace_config,
        registry=env_index_registry,
        env_discovery=env_discovery_svc,
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

    agent_probe_svc = providers.Factory(
        _lazy("winter_cli.modules.doctor.agent_probe_service:AgentProbeService"),
        config=workspace_config,
        fs=fs,
        manifest_loader=extension_manifest_loader,
        agent_copy_inspector=agent_copy_inspector,
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
        agent_probe_svc=agent_probe_svc,
        env_bands_probe_svc=env_bands_probe_svc,
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

    # ── agents: read-only agent model/effort resolution matrix ──────────────

    agent_matrix_svc = providers.Factory(
        _lazy("winter_cli.modules.agents.agent_matrix_service:AgentMatrixService"),
        workspace_config_svc=workspace_config_svc,
        agent_copy_inspector=agent_copy_inspector,
        # Built per `build()` from that call's reloaded config, with the same
        # `fs` the `repo_factory` singleton binding uses.
        repo_factory_for=providers.Factory(RepositoryFactory, fs=fs).provider,
    )

    stream_agent_matrix_reporter = providers.Factory(
        _lazy("winter_cli.modules.agents.matrix_reporter:StreamAgentMatrixReporter"),
        click=providers.Object(click),
        cli_output=cli_output_svc,
    )

    json_agent_matrix_reporter = providers.Factory(
        _lazy("winter_cli.modules.agents.matrix_reporter:JsonAgentMatrixReporter"),
        click=providers.Object(click),
    )

    agents_handler = providers.Factory(
        _lazy("winter_cli.modules.agents.handler:AgentsHandler"),
        matrix_svc=agent_matrix_svc,
        stream_reporter=stream_agent_matrix_reporter,
        json_reporter=json_agent_matrix_reporter,
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
        service_prefix=workspace_config.provided.service_prefix,
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
        service_prefix=workspace_config.provided.service_prefix,
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

    service_fan_out_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_fan_out_service:ServiceFanOutService"),
        subprocess_runner=subprocess_runner,
        workspace_root=workspace_config.provided.workspace_root,
        service_prefix=workspace_config.provided.service_prefix,
        tracer=command_tracer,
        manifest_collector=service_manifest_collector_svc,
        env_provisioner=env_provisioner,
        reporter=stream_service_reporter,
    )

    service_status_matrix_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_status_matrix_service:ServiceStatusMatrixService"),
        subprocess_runner=subprocess_runner,
        describe_service=service_describe_svc,
        env_provisioner=env_provisioner,
        status_parser=status_document_parser,
        env_index_registry=env_index_registry,
        workspace_root=workspace_config.provided.workspace_root,
        service_prefix=workspace_config.provided.service_prefix,
    )

    service_dispatch_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_dispatch_service:ServiceDispatchService"),
        subprocess_runner=subprocess_runner,
        orchestrator_resolver=service_orchestrator_resolver,
        fan_out_service=service_fan_out_svc,
        describe_service=service_describe_svc,
        matrix_service=service_status_matrix_svc,
        workspace_root=workspace_config.provided.workspace_root,
        service_prefix=workspace_config.provided.service_prefix,
        reporter=stream_service_reporter,
    )

    service_logs_svc = providers.Factory(
        _lazy("winter_cli.modules.service.service_logs_service:ServiceLogsService"),
        subprocess_runner=subprocess_runner,
        orchestrator_resolver=service_orchestrator_resolver,
        describe_service=service_describe_svc,
        workspace_root=workspace_config.provided.workspace_root,
        service_prefix=workspace_config.provided.service_prefix,
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
        tracer=command_tracer,
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
        tracer=command_tracer,
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
        workspace_repo=worktree_repo,
        repo_factory=repo_factory,
        workspace=workspace,
    )

    destroy_svc = providers.Factory(
        DestroyService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_hook_svc=extension_hook_svc,
        fs=fs,
        git_repo=git_repo,
        registry=env_index_registry,
        exclude_locator=workspace_exclude_locator,
        provision_svc=provision_svc,
        nested_svc=nested_workspace_svc,
    )

    destroy_handler = providers.Factory(
        DestroyHandler,
        destroy_service=destroy_svc,
        reporter_factory=reporter_factory,
        workspace_repo=worktree_repo,
        repo_factory=repo_factory,
        workspace=workspace,
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

    # Gitignore-resolution seam for the file-size check's markdown discovery:
    # confines `git check-ignore` so a repo's own ignored trees never become
    # lint candidates.
    lint_gitignore_repo = providers.Singleton(
        _lazy("winter_cli.modules.lint.internal.gitignore_repository:GitIgnoreRepository"),
        subprocess_runner=subprocess_runner,
    )

    core_lint_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.core_lint_service:CoreLintService"),
        workspace_root=workspace_config.provided.workspace_root,
        fs=fs,
        subprocess_runner=subprocess_runner,
        winter_cli_path=winter_cli_path,
        script_path=extractability_script_path,
        gitignore_repo=lint_gitignore_repo,
        file_size_config=workspace_config.provided.file_size_lint,
        orchestrator_resolver=service_orchestrator_resolver,
        catalog_service=service_catalog_svc,
        service_prefix=workspace_config.provided.service_prefix,
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

    # Central `[lint.ignore]` filter — applied once to the flattened findings of
    # every source, so no lint script has to know ignore config exists.
    lint_ignore_svc = providers.Factory(
        _lazy("winter_cli.modules.lint.ignore_service:LintIgnoreService"),
        workspace_root=workspace_config.provided.workspace_root,
        fs=fs,
        config_file_reader=config_file_reader,
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
        ignore_svc=lint_ignore_svc,
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
        snapshot_svc=dashboard_snapshot_svc,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
        session_tracer=command_tracer,
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
        session_tracer=command_tracer,
    )

    standalone_detail_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.standalone_detail:StandaloneDetailScreen"),
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
        session_tracer=command_tracer,
    )

    error_log_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.error_log:ErrorLogScreen"),
        error_log=error_log_svc,
    )

    # Each run resolves `InitService` from a fresh, non-interactive Container
    # (`FreshInitServiceFactory`, handed this container's tracer), so `ws init` from
    # the dashboard sees the current config rather than this container's launch-time
    # singleton. A singleton so every Agent matrix screen shares its
    # one-run-at-a-time lock.
    fresh_init_service_factory = providers.Singleton(FreshInitServiceFactory, command_tracer=command_tracer)

    ws_init_runner = providers.Singleton(
        _lazy("winter_cli.modules.tui.ws_init_runner:WorkspaceInitRunner"),
        init_svc_factory=fresh_init_service_factory,
    )

    agent_matrix_screen = providers.Factory(
        _lazy("winter_cli.modules.tui.screens.agent_matrix:AgentMatrixScreen"),
        matrix_svc=agent_matrix_svc,
        error_log=error_log_svc,
        keybinding_resolver=keybinding_resolver,
        ws_init_runner=ws_init_runner,
        session_tracer=command_tracer,
    )
