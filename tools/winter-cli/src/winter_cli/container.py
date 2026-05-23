from __future__ import annotations

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
from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.tui.screens.error_log import ErrorLogScreen
from winter_cli.modules.tui.screens.workspace import WorkspaceScreen
from winter_cli.modules.tui.screens.worktree_detail import WorktreeDetailScreen
from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.drift import DriftWarningService
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.fetch_reporter import JsonFetchReporter, StreamFetchReporter
from winter_cli.modules.workspace.handlers.destroy_handler import DestroyHandler
from winter_cli.modules.workspace.handlers.init_handler import InitHandler
from winter_cli.modules.workspace.handlers.repo_handler import RepoHandler
from winter_cli.modules.workspace.handlers.workspace_handler import WorkspaceHandler
from winter_cli.modules.workspace.init_reporter import JsonReporter, StreamReporter
from winter_cli.modules.workspace.init_service import InitService
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.gitpython_repository import GitPythonRepository
from winter_cli.modules.workspace.internal.read_workspace_repository import ReadWorkspaceRepository
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.write_repo_repository import WriteRepoRepository
from winter_cli.modules.workspace.prune_service import PruneService
from winter_cli.modules.workspace.pull_reporter import JsonPullReporter, StreamPullReporter
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_service import WorkspaceService
from winter_cli.plugins.internal.importlib_plugin_loader import ImportlibPluginLoader
from winter_cli.plugins.loader import PluginRegistry


class Container(containers.DeclarativeContainer):
    """DI container for the winter CLI."""

    __self__ = providers.Self()

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
        ),
    )

    repo_factory = providers.Singleton(
        RepositoryFactory,
        config=workspace_config,
    )

    plugin_registry = providers.Singleton(
        PluginRegistry.load,
        workspace=workspace,
        standalone_repos=repo_factory.provided.get_standalone_repos.call(),
    )

    worktree_repo = providers.Factory(ReadWorkspaceRepository, error_factory=repo_error_factory)

    drift_warning_svc = providers.Factory(
        DriftWarningService,
        workspace=workspace,
        repo_factory=repo_factory,
        fs=fs,
        click=providers.Object(click),
    )

    workspace_svc = providers.Factory(
        WorkspaceService,
        worktree_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        git_ops=git_ops_svc,
    )

    extension_svc = providers.Singleton(
        ExtensionService,
        config=workspace_config,
        fs=fs,
        config_file_reader=config_file_reader,
        subprocess_runner=subprocess_runner,
    )

    prune_svc = providers.Factory(
        PruneService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_svc=extension_svc,
        fs=fs,
        git_repo=git_repo,
    )

    init_svc = providers.Factory(
        InitService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_svc=extension_svc,
        fs=fs,
        subprocess_runner=subprocess_runner,
        git_repo=git_repo,
    )

    destroy_svc = providers.Factory(
        DestroyService,
        config=workspace_config,
        repo_factory=repo_factory,
        extension_svc=extension_svc,
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

    reporter_factory = providers.Singleton(
        ReporterFactory,
        container=__self__,
    )

    workspace_handler = providers.Factory(
        WorkspaceHandler,
        workspace_svc=workspace_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        drift_warning_svc=drift_warning_svc,
        prune_svc=prune_svc,
        reporter_factory=reporter_factory,
        cli_output_svc=cli_output_svc,
        workspace=workspace,
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

    destroy_handler = providers.Factory(
        DestroyHandler,
        destroy_service=destroy_svc,
        reporter_factory=reporter_factory,
    )

    # Session-scoped log buffer for RepoErrors captured during dashboard
    # polling and actions. Singleton so navigating between screens preserves
    # the entries within a single dashboard session.
    error_log_svc = providers.Singleton(ErrorLogService)

    workspace_screen = providers.Factory(
        WorkspaceScreen,
        workspace_svc=workspace_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
    )

    worktree_detail_screen = providers.Factory(
        WorktreeDetailScreen,
        workspace_svc=workspace_svc,
        workspace_repo=worktree_repo,
        repo_repo=repo_repo,
        repo_factory=repo_factory,
        workspace=workspace,
        plugin_registry=plugin_registry,
        error_log=error_log_svc,
    )

    error_log_screen = providers.Factory(
        ErrorLogScreen,
        error_log=error_log_svc,
    )
