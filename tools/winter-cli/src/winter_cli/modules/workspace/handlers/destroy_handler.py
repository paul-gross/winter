from __future__ import annotations

import dataclasses
import sys

import click

from winter_cli.modules.workspace.destroy_service import DestroyService
from winter_cli.modules.workspace.models import FeatureEnvironment, Workspace
from winter_cli.modules.workspace.pattern_match import has_glob, resolve_name_patterns
from winter_cli.modules.workspace.reporter_factory import ReporterFactory
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository


@dataclasses.dataclass
class DestroyParams:
    patterns: list[str]
    force: bool
    strict: bool
    dry_run: bool
    output_json: bool
    no_provision_teardown: bool = False


class DestroyHandler:
    """Handles `winter ws destroy` invocations by selecting a reporter and dispatching to the service.

    `patterns` are env-level globs (bare `<env>`, no `<env>/<repo>` segment —
    destroy always operates on a whole env). Because teardown is irreversible,
    a glob or a multi-env selection prints the resolved env list and asks for
    confirmation before doing anything; `--force` bypasses the prompt (in
    addition to its existing meaning of bypassing the dirty-worktree check).
    A single literal target destroys with no prompt, matching the pre-glob
    behavior exactly.
    """

    def __init__(
        self,
        destroy_service: DestroyService,
        reporter_factory: ReporterFactory,
        workspace_repo: IReadWorkspaceRepository,
        repo_factory: RepositoryFactory,
        workspace: Workspace,
    ) -> None:
        self._destroy_service = destroy_service
        self._reporter_factory = reporter_factory
        self._workspace_repo = workspace_repo
        self._repo_factory = repo_factory
        self._workspace = workspace

    def run(self, params: DestroyParams) -> None:
        envs = self._resolve_envs(params.patterns)
        if not envs:
            click.echo(f"No environments matched: {' '.join(params.patterns)}")
            return

        needs_confirmation = len(params.patterns) > 1 or any(has_glob(p) for p in params.patterns)
        if needs_confirmation and not params.force and not params.dry_run:
            click.echo(f"This will destroy {len(envs)} environment(s): {', '.join(e.name for e in envs)}")
            click.confirm("Continue?", abort=True)

        reporter = self._reporter_factory.get_init_reporter(params.output_json)
        success = True
        for env in envs:
            if not self._destroy_service.destroy_env(
                name=env.name,
                force=params.force,
                strict=params.strict,
                dry_run=params.dry_run,
                reporter=reporter,
                provision_teardown=not params.no_provision_teardown,
            ):
                success = False
        if not success:
            sys.exit(1)

    def _resolve_envs(self, patterns: list[str]) -> list[FeatureEnvironment]:
        """Resolve env-level PATTERNS to `FeatureEnvironment`s, in deterministic order.

        A literal pattern (no glob char) resolves by name directly — even if the
        env doesn't exist on disk — so `winter ws destroy <typo>` still surfaces
        `DestroyService`'s own "env directory not found" error instead of
        silently matching nothing. A glob pattern is expanded against the envs
        discovered on disk, deduped against the literal names.
        """
        literal = {p for p in patterns if not has_glob(p)}
        discovered_by_name: dict[str, FeatureEnvironment] = {}

        def discover_names() -> list[str]:
            project_repos = self._repo_factory.get_project_repos()
            discovered = self._workspace_repo.get_environments(self._workspace, project_repos)
            discovered_by_name.update({env.name: env for env in discovered})
            return [env.name for env in discovered]

        names = resolve_name_patterns(patterns, discover_names)
        return [
            self._workspace_repo.get_environment(self._workspace, name) if name in literal else discovered_by_name[name]
            for name in names
        ]
