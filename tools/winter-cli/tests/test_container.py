from __future__ import annotations

from winter_cli.container import Container
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


def test_container_resolves_restack_providers(container: Container) -> None:
    """The `winter ws restack` dispatch chain wires end-to-end: both services
    and the handler that composes them resolve through the full DI graph."""
    from winter_cli.modules.workspace.handlers.restack_handler import RestackHandler

    assert isinstance(container.env_restack_plan_svc(), EnvRestackPlanService)
    assert isinstance(container.env_restack_svc(), EnvRestackService)
    assert isinstance(container.restack_handler(), RestackHandler)
