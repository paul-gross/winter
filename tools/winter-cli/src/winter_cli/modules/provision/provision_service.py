from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol

import click

from winter_cli.config.models import WorkspaceConfig
from winter_cli.config.workspace import parse_provision
from winter_cli.core.config_file import ConfigError
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.provision.execution_service import HandlerExecutionResult, ProvisionExecutionService
from winter_cli.modules.provision.manifest import PROVISION_SUBTARGETS, ProvisionHandler, ProvisionScope
from winter_cli.modules.provision.provision_reporter import IProvisionReporter
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service-check seam (Phase 5 fills this in)
# ---------------------------------------------------------------------------


class IProvisionServiceCheck(Protocol):
    """Seam for the Phase 5 required-services check.

    Phase 5 must implement a concrete class with this signature and inject it
    into ``ProvisionService`` via the container.  Phase 4 provides a no-op
    stub so the rest of the service compiles and runs without a service
    orchestrator.

    Signature Phase 5 must implement::

        def ensure(
            self,
            handlers: list[ProvisionHandler],
            env_name: str,
            no_service_check: bool,
        ) -> str | None:
            ...

    Return value semantics:
    - Returns a short status string (e.g. ``"skipped"`` or ``"ok"``) that is
      placed verbatim in the ``service_check`` field of ``handler_result``
      JSON events.
    - Returns ``None`` when no ``required_services`` are declared on any
      handler in the list.
    - Raises ``ClickException`` (or lets a ``RepoError`` bubble) when a
      required-service check fails and cannot be remediated.
    """

    def ensure(
        self,
        handlers: list[ProvisionHandler],
        env_name: str,
        no_service_check: bool,
    ) -> str | None: ...


@dataclasses.dataclass
class NoOpServiceCheck:
    """Phase 4 stub — always returns 'skipped' (no orchestrator wired yet)."""

    def ensure(
        self,
        handlers: list[ProvisionHandler],
        env_name: str,
        no_service_check: bool,
    ) -> str | None:
        has_required = any(h.required_services for h in handlers)
        if not has_required:
            return None
        return "skipped"


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------

_SCOPE_RANK: dict[ProvisionScope, int] = {
    ProvisionScope.workspace: 0,
    ProvisionScope.feature_environment: 1,
    ProvisionScope.feature_worktree: 2,
}


def _service_check_preview(handler: ProvisionHandler, env_name: str) -> str | None:
    """Return a human/machine-readable preview of the service check for a handler.

    Returns ``None`` when the handler declares no ``required_services``.
    Returns a comma-separated list of owning scopes that WOULD be checked/started
    (e.g. ``"workspace"`` or ``"alpha"`` or ``"workspace,alpha"``).
    """
    if not handler.required_services:
        return None
    scopes: set[str] = set()
    for token in handler.required_services:
        parts = token.split("/", 1)
        if len(parts) == 2 and parts[0]:
            scopes.add(parts[0])
        else:
            # Malformed token — include it verbatim so the plan is still informative.
            scopes.add(token)
    return ",".join(sorted(scopes))


def _sort_key(handler: ProvisionHandler, index: int) -> tuple[int, int, int]:
    """Sort key for handlers within a single sub-target.

    Ordering rules (lowest value → runs first):
    1. Scope rank: workspace (0) → feature-environment (1) → feature-worktree (2)
    2. Source priority: ``"project"`` (0) before extension handlers (1)
    3. Original declaration index (declaration order as tiebreak)
    """
    scope_rank = _SCOPE_RANK[handler.scope]
    source_is_ext = 0 if handler.source == "project" else 1
    return (scope_rank, source_is_ext, index)


# ---------------------------------------------------------------------------
# ProvisionSummary
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProvisionSummary:
    """Result returned by ``ProvisionService.run()``."""

    status: str  # "ok" | "aborted" | "error"
    aborted_at: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "ok" else 1


# ---------------------------------------------------------------------------
# ProvisionService
# ---------------------------------------------------------------------------


class ProvisionService:
    """Orchestrates provision handler collection, ordering, and execution.

    Responsibilities:
    - Collect handlers from workspace config and extension manifests.
    - Filter by sub-target(s) (full chain or explicit single sub-target).
    - Order handlers within each sub-target (scope-substrate-first, project
      before extension, declaration order as tiebreak).
    - Resolve the correct action per flag combination and run each handler via
      the execution service.
    - Implement abort semantics: a failing apply within a sub-target's handlers
      aborts that sub-target immediately AND skips all remaining sub-targets.
    - Delegate service-check responsibility to the injected seam (Phase 5).
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        execution_svc: ProvisionExecutionService,
        manifest_loader: ExtensionManifestLoader,
        repo_factory: RepositoryFactory,
        service_check: IProvisionServiceCheck,
        fs: IFilesystemReader | None = None,
    ) -> None:
        self._config = config
        self._execution_svc = execution_svc
        self._manifest_loader = manifest_loader
        self._repo_factory = repo_factory
        self._service_check = service_check
        self._fs = fs

    def run(
        self,
        env_name: str,
        subtarget: str | None,
        reset: bool,
        destroy: bool,
        seed: bool,
        no_service_check: bool,
        reporter: IProvisionReporter,
        dry_run: bool = False,
    ) -> ProvisionSummary:
        """Run the provision chain (or a single sub-target) for *env_name*.

        ``subtarget`` is the explicit sub-target name when given, or ``None``
        for the full dependency→resource→data chain.

        When ``dry_run`` is ``True``, no handler scripts are executed, no
        services are started, and the reporter receives ``plan_handler`` events
        describing the ordered list of handlers that would run.
        """
        # Validate the env directory exists before collecting handlers, so a
        # typo gives one clean error instead of per-handler OSError on missing cwds.
        env_dir = self._config.workspace_root / env_name
        env_dir_exists = self._fs.is_dir(env_dir) if self._fs is not None else env_dir.is_dir()
        if not env_dir_exists:
            raise click.ClickException(
                f"Environment {env_name!r} does not exist "
                f"(expected directory: {env_dir}). "
                f"Run 'winter ws init {env_name}' to create it."
            )

        subtargets_to_run = self._resolve_subtargets(subtarget, seed)
        reporter.provision_started(env_name, list(subtargets_to_run))

        all_handlers = self._collect_all_handlers()

        if dry_run:
            return self._run_dry(
                subtargets_to_run=subtargets_to_run,
                all_handlers=all_handlers,
                env_name=env_name,
                reset=reset,
                destroy=destroy,
                reporter=reporter,
            )

        for st in subtargets_to_run:
            handlers = self._filter_and_sort(all_handlers, st)

            reporter.subtarget_started(st)

            if not handlers:
                reporter.no_handlers(st)
                continue

            # Phase 5 seam — service check before resource/data handlers run.
            service_check_result = self._service_check.ensure(handlers, env_name, no_service_check)

            result = self._run_subtarget(
                st=st,
                handlers=handlers,
                env_name=env_name,
                reset=reset,
                destroy=destroy,
                service_check_result=service_check_result,
                reporter=reporter,
            )
            if result == "aborted":
                reporter.provision_finished(status="aborted", aborted_at=st)
                return ProvisionSummary(status="aborted", aborted_at=st)
            if result == "error":
                reporter.provision_finished(status="error", aborted_at=None)
                return ProvisionSummary(status="error")

        reporter.provision_finished(status="ok", aborted_at=None)
        return ProvisionSummary(status="ok")

    # ── Dry-run path ──────────────────────────────────────────────────────

    def _run_dry(
        self,
        *,
        subtargets_to_run: tuple[str, ...],
        all_handlers: list[ProvisionHandler],
        env_name: str,
        reset: bool,
        destroy: bool,
        reporter: IProvisionReporter,
    ) -> ProvisionSummary:
        """Emit plan events for every handler that would run; no scripts executed."""
        for st in subtargets_to_run:
            handlers = self._filter_and_sort(all_handlers, st)

            reporter.subtarget_started(st)

            if not handlers:
                reporter.no_handlers(st)
                continue

            for handler in handlers:
                actions = self._resolve_dry_actions(handler, reset=reset, destroy=destroy)
                service_check_preview = _service_check_preview(handler, env_name)
                for action, script in actions:
                    reporter.plan_handler(
                        subtarget=handler.subtarget,
                        scope=handler.scope.value,
                        source=handler.source,
                        script=script,
                        action=action,
                        required_services=list(handler.required_services),
                        service_check_preview=service_check_preview,
                    )

        reporter.provision_finished(status="ok", aborted_at=None)
        return ProvisionSummary(status="ok")

    @staticmethod
    def _resolve_dry_actions(
        handler: ProvisionHandler,
        reset: bool,
        destroy: bool,
    ) -> list[tuple[str, str]]:
        """Return the (action, script) pairs that would run for this handler.

        Mirrors the real action-resolution logic in ``_run_handler_with_action``
        / ``_run_destroy`` / ``_run_reset`` but returns the plan without executing.
        """
        if destroy:
            if handler.destroy is not None:
                return [("destroy", handler.destroy)]
            # No destroy script — would warn and no-op.
            return []

        if reset:
            if handler.reset is not None:
                return [("reset", handler.reset)]
            if handler.destroy is not None:
                # Compose: destroy then apply.
                return [("destroy", handler.destroy), ("apply", handler.apply)]
            # No reset and no destroy — would warn and degrade to apply.
            return [("apply", handler.apply)]

        # Bare apply.
        return [("apply", handler.apply)]

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_subtargets(subtarget: str | None, seed: bool) -> tuple[str, ...]:
        """Return the ordered list of sub-targets to run."""
        if seed:
            # --seed on resource: run resource apply, then data apply.
            # The caller has already validated subtarget == "resource".
            return ("resource", "data")
        if subtarget is not None:
            return (subtarget,)
        return PROVISION_SUBTARGETS

    def _collect_all_handlers(self) -> list[ProvisionHandler]:
        """Collect workspace-config handlers + every extension's handlers."""
        handlers: list[ProvisionHandler] = []

        # 1) Workspace-config (source="project")
        try:
            handlers.extend(parse_provision(self._config, source="project"))
        except ConfigError as exc:
            raise click.ClickException(f"Malformed workspace [provision] config: {exc}") from exc

        # 2) Extension manifests — iterate standalone repos declared in config.
        for repo in self._repo_factory.get_standalone_repos():
            manifest_path = self._config.workspace_root / repo.name / EXT_MANIFEST
            # Check existence: prefer the injected fs seam (testable), fall
            # back to real pathlib for production wiring where fs is None.
            exists = self._fs.is_file(manifest_path) if self._fs is not None else manifest_path.exists()
            if not exists:
                continue
            try:
                manifest = self._manifest_loader.load(repo, manifest_path)
                handlers.extend(manifest.provision)
            except RepoError as exc:
                logger.warning("Skipping extension %r provision manifest: %s", repo.name, exc)

        return handlers

    def _filter_and_sort(
        self,
        all_handlers: list[ProvisionHandler],
        subtarget: str,
    ) -> list[ProvisionHandler]:
        """Filter *all_handlers* to *subtarget* and sort them."""
        filtered = [h for h in all_handlers if h.subtarget == subtarget]
        # Build a position map keyed by object identity so _sort_key can use
        # each handler's index in all_handlers as a stable tiebreak — this
        # preserves declaration order across both project and extension handlers.
        original_indices = {id(h): i for i, h in enumerate(all_handlers)}
        return sorted(filtered, key=lambda h: _sort_key(h, original_indices[id(h)]))

    def _run_subtarget(
        self,
        *,
        st: str,
        handlers: list[ProvisionHandler],
        env_name: str,
        reset: bool,
        destroy: bool,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Run all handlers for *st*.

        Returns ``"aborted"`` when an apply failure should abort the remaining
        sub-targets, ``"error"`` when a destroy/reset failure ends the run
        non-ok, or ``None`` when all handlers succeeded.
        """
        for handler in handlers:
            signal = self._run_handler_with_action(
                handler=handler,
                env_name=env_name,
                reset=reset,
                destroy=destroy,
                service_check_result=service_check_result,
                reporter=reporter,
            )
            if signal is not None:
                return signal
        return None

    def _run_handler_with_action(
        self,
        *,
        handler: ProvisionHandler,
        env_name: str,
        reset: bool,
        destroy: bool,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Run a single handler, resolving the action.

        Returns ``"aborted"`` for an apply failure (abort the remaining
        sub-targets), ``"error"`` for a destroy/reset failure (end the run
        non-ok without chain-abort semantics), or ``None`` on success.
        """
        scope_str = handler.scope.value
        source_str = handler.source
        st = handler.subtarget

        if destroy:
            return self._run_destroy(
                handler=handler,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        if reset:
            return self._run_reset(
                handler=handler,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        # bare apply
        return self._run_action(
            handler=handler,
            action="apply",
            env_name=env_name,
            scope_str=scope_str,
            source_str=source_str,
            st=st,
            service_check_result=service_check_result,
            reporter=reporter,
        )

    def _run_destroy(
        self,
        *,
        handler: ProvisionHandler,
        env_name: str,
        scope_str: str,
        source_str: str,
        st: str,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        if handler.destroy is None:
            reporter.handler_warn(
                subtarget=st,
                scope=scope_str,
                source=source_str,
                message="no destroy script declared — skipping",
            )
            return None
        return self._run_action(
            handler=handler,
            action="destroy",
            env_name=env_name,
            scope_str=scope_str,
            source_str=source_str,
            st=st,
            service_check_result=service_check_result,
            reporter=reporter,
        )

    def _run_reset(
        self,
        *,
        handler: ProvisionHandler,
        env_name: str,
        scope_str: str,
        source_str: str,
        st: str,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Run reset: declared reset → run reset; else destroy+apply; else warn+apply."""
        if handler.reset is not None:
            return self._run_action(
                handler=handler,
                action="reset",
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        if handler.destroy is not None:
            # Compose destroy then apply.  A failing destroy leg surfaces as
            # "error" (via _run_action) and prevents apply from running.
            signal = self._run_action(
                handler=handler,
                action="destroy",
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )
            if signal is not None:
                return signal
            return self._run_action(
                handler=handler,
                action="apply",
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        # No reset and no destroy — warn, degrade to re-apply.
        reporter.handler_warn(
            subtarget=st,
            scope=scope_str,
            source=source_str,
            message="no reset or destroy script declared — degrading to apply",
        )
        return self._run_action(
            handler=handler,
            action="apply",
            env_name=env_name,
            scope_str=scope_str,
            source_str=source_str,
            st=st,
            service_check_result=service_check_result,
            reporter=reporter,
        )

    def _run_action(
        self,
        *,
        handler: ProvisionHandler,
        action: str,
        env_name: str,
        scope_str: str,
        source_str: str,
        st: str,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Execute one action via the execution service.

        Returns ``"aborted"`` when an apply failure should abort the remaining
        sub-targets, ``"error"`` when a destroy or reset script exits non-zero,
        or ``None`` when the action succeeded.
        """
        result: HandlerExecutionResult = self._execution_svc.run_handler(handler, action, env_name, reporter)
        runs_json: list[dict[str, Any]] = [{"cwd": str(r.cwd), "exit_status": r.exit_code} for r in result.runs]
        overall_exit = 0 if result.ok else 1
        reporter.handler_result(
            subtarget=st,
            scope=scope_str,
            source=source_str,
            action=action,
            service_check=service_check_result,
            runs=runs_json,
            exit_status=overall_exit,
        )
        if not result.ok:
            if action == "apply":
                # Apply failure aborts the remaining sub-targets in the chain.
                return "aborted"
            # Destroy or reset failure: end the run with error status without
            # chain-abort semantics (destroy/reset target a single sub-target).
            return "error"
        return None
