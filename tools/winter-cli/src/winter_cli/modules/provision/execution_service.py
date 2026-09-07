from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, assert_never

import click

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.extension_invocation import build_extension_env
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.provision.manifest import ProvisionAction, ProvisionHandler, ProvisionScope
from winter_cli.modules.workspace.env_index import build_env_trio
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

logger = logging.getLogger(__name__)

# The source label used for workspace-config (project) handlers.
# Mirrors WORKSPACE_SOURCE used in lint/doctor services.
PROJECT_SOURCE = "project"


class IProvisionOutputSink(Protocol):
    """Minimal output sink for provision script execution.

    Phase 4 wraps this with its own Stream/Json reporter pair.  Services that
    only need execution (no JSON/stream distinction) inject any object that
    satisfies this Protocol.
    """

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        """Called immediately before a script subprocess is launched."""
        ...

    def execution_output_line(self, label: str, line: str) -> None:
        """Called for each line of stdout/stderr emitted by the script."""
        ...

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        """Called after the subprocess exits."""
        ...

    def execution_error(self, label: str, error: str) -> None:
        """Called when a command cannot be launched (e.g. OSError spawning sh)."""
        ...


@dataclass(frozen=True)
class SingleRunResult:
    """Result for one concrete script invocation (one cwd, one process)."""

    cwd: Path
    exit_code: int


@dataclass(frozen=True)
class HandlerExecutionResult:
    """Aggregated result for running one ``ProvisionHandler`` action.

    For ``workspace`` and ``feature-environment`` scope a single run is
    produced.  For ``feature-worktree`` scope one ``SingleRunResult`` per
    project worktree appears in ``runs``, in the order the worktrees were
    visited.

    ``ok`` is True when every run exited 0 (or when ``runs`` is empty).
    """

    handler: ProvisionHandler
    action: ProvisionAction
    runs: tuple[SingleRunResult, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        return all(r.exit_code == 0 for r in self.runs)


class ProvisionExecutionService:
    """Runs a single ``ProvisionHandler`` action at the correct cwd with the
    correct env vars.

    Execution only — no ordering, no chain composition, no service check.
    Those live in Phase 4's ``ProvisionService``.

    Source-root / cwd rules:
    - ``project`` source → source root = workspace root; for workspace scope
      there is no ``WINTER_EXT_DIR`` / ``WINTER_EXT_PREFIX`` (workspace-config
      handlers are not extensions), so we pass ``ext_dir=workspace_root`` and
      ``prefix=PROJECT_SOURCE`` as neutral values that still satisfy
      ``build_extension_env``'s signature.
    - Extension source → source root = the extension repo's on-disk path;
      ``WINTER_EXT_DIR`` / ``WINTER_EXT_PREFIX`` / ``WINTER_EXT_CONFIG_DIR``
      are set from that repo's manifest, mirroring the hook service.

    cwd is set by scope, independently of the source root:
    - ``workspace``          → workspace root
    - ``feature-environment`` → ``<workspace_root>/<env>``
    - ``feature-worktree``   → ``<workspace_root>/<env>/<repo.name>`` per project repo
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemWriter,
        subprocess_runner: ISubprocessRunner,
        manifest_loader: ExtensionManifestLoader,
        repo_factory: RepositoryFactory,
        registry: IEnvIndexRegistry | None = None,
    ) -> None:
        self._config = config
        self._fs = fs
        self._subprocess = subprocess_runner
        self._manifest_loader = manifest_loader
        self._repo_factory = repo_factory
        self._registry = registry

    def run_handler(
        self,
        handler: ProvisionHandler,
        action: ProvisionAction,
        env_name: str,
        sink: IProvisionOutputSink,
    ) -> HandlerExecutionResult:
        """Run one handler's named action and return a structured result.

        The caller is responsible for ensuring the action's command tuple is
        non-None on the handler before calling here (Phase 4 owns the
        decompose/warn/degrade decisions). ``sink`` still speaks in the
        action's string value — only this service and its caller deal in
        ``ProvisionAction``.

        Returns ``HandlerExecutionResult`` with all per-run outcomes.  An
        OSError launching sh is captured in ``HandlerExecutionResult.error``
        and ``ok=False``; no exception is raised.

        A ``click.ClickException`` from ``_resolve_cwds`` (a missing project
        worktree for a ``feature-environment`` handler) is a per-handler
        problem, not a whole-run precondition failure. Under
        ``ProvisionAction.clean`` — whose contract is best-effort — it is
        caught here and folded into ``HandlerExecutionResult.error`` exactly
        like a ``RepoError`` from source resolution, so the caller's existing
        ``clean_error`` degrade path (report on this handler's line, keep
        going through siblings and remaining sub-targets/envs) applies
        without ``ProvisionService`` needing to know why the cwd could not be
        resolved. For every other action the exception still propagates
        uncaught — a missing worktree is a hard error for apply/destroy/reset,
        unchanged from before.
        """
        action_str = action.value
        commands = self._resolve_script(handler, action)
        if commands is None:
            error = f"handler for {handler.subtarget!r} has no script for action {action_str!r}"
            logger.warning("%s", error)
            return HandlerExecutionResult(handler=handler, action=action, error=error)

        try:
            _source_root, base_env = self._resolve_source(handler)
        except RepoError as exc:
            error = str(exc)
            sink.execution_error(_handler_label(handler), error)
            return HandlerExecutionResult(handler=handler, action=action, error=error)

        try:
            cwds = self._resolve_cwds(handler, env_name)
        except click.ClickException as exc:
            if action is not ProvisionAction.clean:
                raise
            error = str(exc)
            sink.execution_error(_handler_label(handler), error)
            return HandlerExecutionResult(handler=handler, action=action, error=error)
        runs: list[SingleRunResult] = []

        for cwd in cwds:
            env = dict(base_env)
            if handler.scope in (
                ProvisionScope.feature_environment,
                ProvisionScope.feature_worktree,
            ):
                env.update(build_env_trio(env_name, self._config, self._registry))

            label = _handler_label(handler)
            # execution_started / execution_completed bracket the whole cwd's
            # command sequence, not each individual command, so the reporter
            # sees one started/completed pair per cwd regardless of how many
            # commands the tuple contains.
            sink.execution_started(label, action_str, cwd)
            cwd_exit_code = 0
            try:
                for command in commands:
                    with self._subprocess.popen(["sh", "-c", command], cwd=cwd, env=env) as proc:
                        for line in proc.stdout_lines:
                            sink.execution_output_line(label, line)
                        cwd_exit_code = proc.wait()
                    if cwd_exit_code != 0:
                        # Stop at first failing command within this cwd.
                        break
            except OSError as exc:
                error = f"provision command — {exc}"
                sink.execution_error(label, error)
                return HandlerExecutionResult(handler=handler, action=action, runs=tuple(runs), error=error)
            sink.execution_completed(label, action_str, cwd_exit_code)
            runs.append(SingleRunResult(cwd=cwd, exit_code=cwd_exit_code))

        return HandlerExecutionResult(handler=handler, action=action, runs=tuple(runs))

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _resolve_script(handler: ProvisionHandler, action: ProvisionAction) -> tuple[str, ...] | None:
        """Return the command tuple for the named action.

        Dispatches exhaustively over ``ProvisionAction`` so a member added to
        that enum without a case here fails loudly (``pyright`` flags the
        ``assert_never``) instead of silently falling through to a handler's
        destructive ``clean`` command.
        """
        if action is ProvisionAction.apply:
            return handler.apply
        if action is ProvisionAction.destroy:
            return handler.destroy
        if action is ProvisionAction.reset:
            return handler.reset
        if action is ProvisionAction.clean:
            return handler.clean
        assert_never(action)

    def _resolve_source(
        self,
        handler: ProvisionHandler,
    ) -> tuple[Path, dict[str, str]]:
        """Return ``(source_root, base_env)`` for this handler.

        For project-source handlers the source root is the workspace root.
        For extension handlers the source root is the extension repo directory.
        ``base_env`` always contains the four ``build_extension_env`` vars.
        """
        workspace_root = self._config.workspace_root

        if handler.source == PROJECT_SOURCE:
            # Workspace-config handler: commands run relative to the workspace root.
            # No real WINTER_EXT_DIR / WINTER_EXT_PREFIX semantics, but we
            # still call build_extension_env so downstream scripts see the
            # four standard vars (WINTER_WORKSPACE_DIR is the useful one here).
            config_dir = workspace_root / ".winter" / "config"
            base_env = build_extension_env(
                workspace_root=workspace_root,
                ext_dir=workspace_root,
                prefix=PROJECT_SOURCE,
                config_dir=config_dir,
                service_prefix=self._config.service_prefix,
            )
            return workspace_root, base_env

        # Extension handler: resolve the standalone repo by its source label (prefix).
        ext_repo = self._find_extension(handler.source)
        if ext_repo is None:
            raise RepoError(
                f"provision handler declares source={handler.source!r} but no installed "
                f"extension with that prefix was found"
            )

        manifest_path = ext_repo.path / EXT_MANIFEST
        if not self._fs.is_file(manifest_path):
            raise RepoError(f"extension {handler.source!r}: {EXT_MANIFEST} not found at {manifest_path}")
        manifest = self._manifest_loader.load(ext_repo, manifest_path)

        config_dir = (
            ext_repo.config_dir
            if ext_repo.config_dir is not None
            else (workspace_root / ".winter" / "config" / ext_repo.name)
        )
        base_env = build_extension_env(
            workspace_root=workspace_root,
            ext_dir=ext_repo.path,
            prefix=manifest.prefix,
            config_dir=config_dir,
            service_prefix=self._config.service_prefix,
        )
        return ext_repo.path, base_env

    def _find_extension(self, source_label: str) -> StandaloneRepository | None:
        """Find an extension-eligible repo whose resolved prefix matches *source_label*.

        Extension-eligible repos are standalones plus project repos carrying a
        root `winter-ext.toml`, so a provision handler collected from a
        project-repo extension (see `ProvisionService._collect_all_handlers`)
        resolves back to its `projects/<name>/` checkout here too.
        """
        for repo in self._repo_factory.get_extension_repos():
            manifest_path = repo.path / EXT_MANIFEST
            if not self._fs.is_file(manifest_path):
                continue
            try:
                manifest = self._manifest_loader.load(repo, manifest_path)
                if manifest.prefix == source_label:
                    return repo
            except RepoError:
                continue
        return None

    def _resolve_cwds(self, handler: ProvisionHandler, env_name: str) -> list[Path]:
        """Return the list of cwds to run the script in for the given handler.

        For ``feature-environment`` handlers with a ``project`` field, returns
        a single-element list containing ``<workspace>/<env>/<project>/``.
        Raises ``click.ClickException`` with a hard error when that directory
        does not exist (missing worktree for the target env). This is a
        per-handler resolution failure, not a whole-run precondition: the
        caller, ``run_handler``, catches it and folds it into
        ``HandlerExecutionResult.error`` when the action is
        ``ProvisionAction.clean`` (best-effort — the run keeps going through
        siblings and remaining sub-targets/envs); for every other action it
        propagates as the hard error it already was.
        """
        workspace_root = self._config.workspace_root
        scope = handler.scope
        if scope is ProvisionScope.workspace:
            return [workspace_root]
        if scope is ProvisionScope.feature_environment:
            if handler.project is not None:
                project_cwd = workspace_root / env_name / handler.project
                if not self._fs.is_dir(project_cwd):
                    raise click.ClickException(
                        f"provision handler requires project {handler.project!r} "
                        f"but its worktree does not exist in env {env_name!r} "
                        f"(expected: {project_cwd}). "
                        f"Run 'winter ws init {env_name}' to create worktrees."
                    )
                return [project_cwd]
            return [workspace_root / env_name]
        # feature-worktree: one cwd per project repo in the env
        return [workspace_root / env_name / repo.name for repo in self._repo_factory.get_project_repos()]


def _handler_label(handler: ProvisionHandler) -> str:
    """Human-readable label for a handler, used in reporter calls."""
    return f"{handler.source}/{handler.subtarget}[{handler.scope.value}]"
