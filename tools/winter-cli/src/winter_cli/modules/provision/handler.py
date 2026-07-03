from __future__ import annotations

import dataclasses
import sys

import click

from winter_cli.modules.provision.provision_reporter import (
    IProvisionReporter,
    JsonProvisionReporter,
    StreamProvisionReporter,
)
from winter_cli.modules.provision.provision_service import ProvisionService
from winter_cli.modules.workspace.models import Workspace
from winter_cli.modules.workspace.pattern_match import resolve_name_patterns
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository


@dataclasses.dataclass
class ProvisionParams:
    """Parsed parameters for a ``winter provision`` invocation."""

    patterns: list[str]
    subtarget: str | None = None
    reset: bool = False
    destroy: bool = False
    seed: bool = False
    no_service_check: bool = False
    dry_run: bool = False
    output_json: bool = False


class ProvisionCommandHandler:
    """Dispatches ``winter provision`` runs to the service with the right reporter.

    Named ``ProvisionCommandHandler`` (not ``ProvisionHandler``) to avoid
    collision with the manifest dataclass ``ProvisionHandler`` in
    ``modules.provision.manifest``.

    ``patterns`` are env-level globs (bare ``<env>``, no ``<env>/<repo>``
    segment — provision always operates on a whole env). Each matched env is
    provisioned in turn against the same ``ProvisionService``, which still
    only knows how to provision one env per call; the fan-out and ordering
    live here.
    """

    def __init__(
        self,
        provision_service: ProvisionService,
        stream_reporter: StreamProvisionReporter,
        json_reporter: JsonProvisionReporter,
        workspace_repo: IReadWorkspaceRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
    ) -> None:
        self._provision_service = provision_service
        self._stream_reporter = stream_reporter
        self._json_reporter = json_reporter
        self._workspace_repo = workspace_repo
        self._repo_factory = repo_factory
        self._workspace = workspace

    def run(self, params: ProvisionParams) -> None:
        reporter: IProvisionReporter = self._json_reporter if params.output_json else self._stream_reporter
        envs = self._resolve_envs(params.patterns)
        if not envs:
            click.echo(f"No environments matched: {' '.join(params.patterns)}")
            return

        exit_code = 0
        for env_name in envs:
            summary = self._provision_service.run(
                env_name=env_name,
                subtarget=params.subtarget,
                reset=params.reset,
                destroy=params.destroy,
                seed=params.seed,
                no_service_check=params.no_service_check,
                reporter=reporter,  # type: ignore[arg-type]
                dry_run=params.dry_run,
            )
            if summary.exit_code != 0:
                exit_code = summary.exit_code
        if exit_code != 0:
            sys.exit(exit_code)

    def _resolve_envs(self, patterns: list[str]) -> list[str]:
        """Resolve env-level PATTERNS to concrete env names, in deterministic order.

        A literal pattern (no glob char) is always included verbatim — even if
        the env doesn't exist on disk yet — so `winter provision <typo>` still
        surfaces `ProvisionService`'s own "environment does not exist" error
        instead of silently matching nothing. A glob pattern is expanded
        against the envs discovered on disk, deduped against the literal
        names so a mixed invocation never provisions the same env twice.
        """

        def discover_names() -> list[str]:
            project_repos = self._repo_factory.get_project_repos()
            discovered = self._workspace_repo.get_environments(self._workspace, project_repos)
            return [env.name for env in discovered]

        return resolve_name_patterns(patterns, discover_names)
