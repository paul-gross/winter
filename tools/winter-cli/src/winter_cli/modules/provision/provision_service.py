from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol, assert_never

import click

from winter_cli.config.models import WorkspaceConfig
from winter_cli.config.workspace import parse_provision
from winter_cli.core.config_file import ConfigError
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.provision.execution_service import HandlerExecutionResult, ProvisionExecutionService
from winter_cli.modules.provision.manifest import (
    PROVISION_SUBTARGETS,
    SELECTOR_SCOPE_TOKENS,
    ProvisionAction,
    ProvisionHandler,
    ProvisionScope,
)
from winter_cli.modules.provision.provision_reporter import IProvisionReporter
from winter_cli.modules.workspace.extension_manifest import EXT_MANIFEST, IExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.repository_factory import IExtensionRepoProvider

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
      A failing ``destroy``/``reset`` ends the run non-ok without running the
      rest of that single targeted sub-target. A failing ``clean`` is
      best-effort: it neither stops the rest of its sub-target nor the
      remaining sub-targets, but the run still ends non-ok. This best-effort
      contract also covers a handler whose cwd cannot be resolved (e.g. a
      ``feature-environment`` handler naming a ``project`` whose worktree is
      missing in this env) — ``ProvisionExecutionService`` folds that failure
      into the handler's result under ``clean`` instead of raising, so it
      degrades exactly like a failing clean script.
    - Delegate service-check responsibility to the injected seam (Phase 5).
    - A ``click.ClickException`` raised by the injected service-check
      (``IProvisionServiceCheck.ensure``) is a precondition failure for the
      whole run, not a per-handler one: it still aborts the run by
      propagating uncaught, but ``provision_finished`` is emitted first so a
      ``--json`` consumer still sees a closing ``finished`` event.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        execution_svc: ProvisionExecutionService,
        manifest_loader: IExtensionManifestLoader,
        repo_factory: IExtensionRepoProvider,
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
        action: ProvisionAction,
        seed: bool,
        no_service_check: bool,
        reporter: IProvisionReporter,
        dry_run: bool = False,
        name_selector: str | None = None,
    ) -> ProvisionSummary:
        """Run the provision chain (or a single sub-target) for *env_name*.

        ``subtarget`` is the explicit sub-target name when given, or ``None``
        for the full dependency→resource→data chain.

        ``action`` selects the run verb: bare ``ProvisionAction.apply``, or
        ``ProvisionAction.destroy`` / ``ProvisionAction.reset`` to narrow a
        single sub-target to that action instead. ``ProvisionAction.clean``
        runs the full chain (or a narrowed sub-target) against each handler's
        declared ``clean`` command; a handler declaring none is filtered out
        at selection rather than run.

        ``name_selector``, when given, is a scope-qualified ``<scope>.<name>``
        token (see ``manifest.SELECTOR_SCOPE_TOKENS``) that narrows the run to
        the single named handler it resolves to — every other handler
        (including siblings in the same sub-target) is skipped. When
        ``subtarget`` is also given, the resolved handler must belong to that
        sub-target or the run aborts with a clear error.

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

        all_handlers = self._collect_all_handlers()

        selected_handler: ProvisionHandler | None = None
        if name_selector is not None:
            selected_handler = self._resolve_selector(name_selector, all_handlers)
            if subtarget is not None and selected_handler.subtarget != subtarget:
                raise click.ClickException(
                    f"provision selector {name_selector!r} resolves to a {selected_handler.subtarget!r} handler, "
                    f"but --stage {subtarget!r} was given."
                )
            subtargets_to_run: tuple[str, ...] = (selected_handler.subtarget,)
        else:
            subtargets_to_run = self._resolve_subtargets(subtarget, seed)

        reporter.provision_started(env_name, list(subtargets_to_run), action)

        if dry_run:
            return self._run_dry(
                subtargets_to_run=subtargets_to_run,
                all_handlers=all_handlers,
                env_name=env_name,
                action=action,
                reporter=reporter,
                selected_handler=selected_handler,
            )

        had_clean_failure = False
        for st in subtargets_to_run:
            if selected_handler is not None:
                handlers = [selected_handler]
            else:
                handlers = self._select_for_action(self._filter_and_sort(all_handlers, st), action)

            reporter.subtarget_started(st)

            if not handlers:
                reporter.no_handlers(st)
                continue

            # Phase 5 seam — service check before resource/data handlers run.
            # A --name selector bypasses the _select_for_action filtering
            # above (handlers is always [selected_handler]), so re-apply the
            # same "does this handler actually have work for this action"
            # filter here — otherwise a clean run naming a handler with no
            # declared clean would start required_services for a handler
            # _run_clean is about to no-op.
            #
            # A ClickException here is a precondition failure for the whole
            # run (unlike a per-handler cwd-resolution failure — see
            # ProvisionExecutionService.run_handler) and legitimately aborts
            # it by propagating uncaught; emit provision_finished first so a
            # --json consumer still gets its closing `finished` event instead
            # of a truncated stream.
            try:
                service_check_result = self._service_check.ensure(
                    self._select_for_action(handlers, action), env_name, no_service_check
                )
            except click.ClickException:
                reporter.provision_finished(status="error", aborted_at=None)
                raise

            result = self._run_subtarget(
                st=st,
                handlers=handlers,
                env_name=env_name,
                action=action,
                service_check_result=service_check_result,
                reporter=reporter,
            )
            if result == "aborted":
                reporter.provision_finished(status="aborted", aborted_at=st)
                return ProvisionSummary(status="aborted", aborted_at=st)
            if result == "error":
                reporter.provision_finished(status="error", aborted_at=None)
                return ProvisionSummary(status="error")
            if result == "clean_error":
                # Best-effort: a failing clean does not stop this sub-target's
                # remaining handlers or the remaining sub-targets — keep going,
                # but the run still ends non-ok.
                had_clean_failure = True

        final_status = "error" if had_clean_failure else "ok"
        reporter.provision_finished(status=final_status, aborted_at=None)
        return ProvisionSummary(status=final_status)

    # ── Dry-run path ──────────────────────────────────────────────────────

    def _run_dry(
        self,
        *,
        subtargets_to_run: tuple[str, ...],
        all_handlers: list[ProvisionHandler],
        env_name: str,
        action: ProvisionAction,
        reporter: IProvisionReporter,
        selected_handler: ProvisionHandler | None = None,
    ) -> ProvisionSummary:
        """Emit plan events for every handler that would run; no scripts executed."""
        for st in subtargets_to_run:
            if selected_handler is not None:
                handlers = [selected_handler]
            else:
                handlers = self._select_for_action(self._filter_and_sort(all_handlers, st), action)

            reporter.subtarget_started(st)

            if not handlers:
                reporter.no_handlers(st)
                continue

            for handler in handlers:
                if action is ProvisionAction.clean and handler.clean is None:
                    # Mirrors the real run's warn for a --name selector naming
                    # a handler with no declared clean (see _run_clean) — the
                    # preview must warn everywhere the real run would.
                    reporter.handler_warn(
                        subtarget=handler.subtarget,
                        scope=handler.scope.value,
                        source=handler.source,
                        message="no clean command declared — skipping",
                    )
                    continue
                planned = self._resolve_dry_actions(handler, action)
                service_check_preview = _service_check_preview(handler, env_name)
                cwd_preview = self._dry_run_cwd(handler, env_name)
                for planned_action, commands in planned:
                    reporter.plan_handler(
                        subtarget=handler.subtarget,
                        scope=handler.scope.value,
                        source=handler.source,
                        commands=list(commands),
                        action=planned_action,
                        required_services=list(handler.required_services),
                        service_check_preview=service_check_preview,
                        cwd=cwd_preview,
                        project=handler.project,
                    )

        reporter.provision_finished(status="ok", aborted_at=None)
        return ProvisionSummary(status="ok")

    def _dry_run_cwd(self, handler: ProvisionHandler, env_name: str) -> str:
        """Describe where *handler*'s action would actually run, for the preview.

        Mirrors ``ProvisionExecutionService._resolve_cwds``'s scope rules
        without touching the filesystem, so previewing a handler whose
        project worktree doesn't exist yet never raises — it only describes
        where the real run would look.

        ``workspace`` scope is spelled out explicitly as the workspace root
        rather than left to the ``scope`` field alone: a manifest declaring
        ``scope = "workspace"`` runs its ``clean``/``apply``/etc. at the
        shared workspace root, not inside *env_name* — the blast radius a
        manifest author most often gets wrong, and the whole reason this
        preview exists.
        """
        workspace_root = self._config.workspace_root
        if handler.scope is ProvisionScope.workspace:
            return f"{workspace_root} (workspace root — shared, not inside env {env_name!r})"
        if handler.scope is ProvisionScope.feature_environment:
            if handler.project is not None:
                return str(workspace_root / env_name / handler.project)
            return str(workspace_root / env_name)
        # feature-worktree: one cwd per project repo in the env.
        return f"{workspace_root / env_name}/<project-repo> (once per project repo)"

    @staticmethod
    def _resolve_dry_actions(
        handler: ProvisionHandler,
        action: ProvisionAction,
    ) -> list[tuple[str, tuple[str, ...]]]:
        """Return the (action, commands) pairs that would run for this handler.

        Mirrors the action-resolution shape of ``_run_handler_with_action`` /
        ``_run_destroy`` / ``_run_reset`` for the plan itself, but the two are
        not fully in parity on warn events: the clean degrade case (a handler
        with no declared ``clean``) is warned by the caller, ``_run_dry``,
        before this is even called — matching ``_run_clean``'s warn. The
        destroy and reset degrade cases below do not carry a matching warn:
        the real run (``_run_destroy``, ``_run_reset``) emits a
        ``handler_warn`` for a missing ``destroy`` script and for a missing
        ``reset``/``destroy`` pair, but the plan returned here is silent for
        both. Closing that gap would change ``--destroy``/``--reset`` dry-run
        output, which is out of scope here.

        Dispatches exhaustively over ``ProvisionAction`` — mirroring
        ``ProvisionExecutionService._resolve_script`` and
        ``_action_verb`` — so a member added to that enum without a case
        here fails loudly (``pyright`` flags the ``assert_never``) instead of
        silently previewing an apply for a verb this plan doesn't know about.
        """
        if action is ProvisionAction.destroy:
            if handler.destroy is not None:
                return [(ProvisionAction.destroy.value, handler.destroy)]
            # No destroy script — the real run warns here; this plan does not.
            return []

        if action is ProvisionAction.reset:
            if handler.reset is not None:
                return [(ProvisionAction.reset.value, handler.reset)]
            if handler.destroy is not None:
                # Compose: destroy then apply.
                return [(ProvisionAction.destroy.value, handler.destroy), (ProvisionAction.apply.value, handler.apply)]
            # No reset and no destroy — the real run warns and degrades to
            # apply; this plan degrades to apply silently.
            return [(ProvisionAction.apply.value, handler.apply)]

        if action is ProvisionAction.clean:
            if handler.clean is not None:
                return [(ProvisionAction.clean.value, handler.clean)]
            # No clean script declared — selection already filters this handler
            # out of a full-chain/subtarget run; a name-selected handler with
            # no clean would warn and no-op, so the plan has no entry for it.
            return []

        if action is ProvisionAction.apply:
            return [(ProvisionAction.apply.value, handler.apply)]
        assert_never(action)

    @staticmethod
    def _select_for_action(handlers: list[ProvisionHandler], action: ProvisionAction) -> list[ProvisionHandler]:
        """Narrow *handlers* to those with a script for *action*, when the action requires it.

        Only ``clean`` filters at selection time: a handler declaring no
        ``clean`` contributes nothing to a clean run, so filtering it out here
        makes a sub-target where nothing declares ``clean`` equivalent to an
        empty sub-target — reported as ``no_handlers`` and starting no service,
        rather than surfacing a per-handler error. ``apply``/``destroy``/``reset``
        keep their existing runtime warn/compose/degrade semantics in
        ``_run_handler_with_action`` and are returned unfiltered.
        """
        if action is ProvisionAction.clean:
            return [h for h in handlers if h.clean is not None]
        return handlers

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

        # 2) Extension manifests — iterate every extension-eligible repo (standalones
        # plus project repos carrying a root winter-ext.toml). Use repo.path (the
        # actual on-disk checkout location) rather than workspace_root/repo.name,
        # since a repo may be installed at a custom path (e.g. .winter/ext/service-tmux
        # for winter-service-tmux, or projects/<name> for a project-repo extension).
        for repo in self._repo_factory.get_extension_repos():
            manifest_path = repo.path / EXT_MANIFEST
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

    @staticmethod
    def _resolve_selector(name_selector: str, all_handlers: list[ProvisionHandler]) -> ProvisionHandler:
        """Resolve a ``<scope>.<name>`` selector to the single handler it names.

        Raises ``click.ClickException`` for a malformed selector, an unknown
        scope token, an unmatched name, or (defensively) a name that
        collides across manifest sources — never a silent no-op.
        """
        scope_token, sep, entry_name = name_selector.partition(".")
        if not sep or not scope_token or not entry_name:
            valid = ", ".join(repr(t) for t in SELECTOR_SCOPE_TOKENS)
            raise click.ClickException(
                f"Invalid provision selector {name_selector!r}. Expected '<scope>.<name>' where <scope> is "
                f"one of: {valid}."
            )

        scope = SELECTOR_SCOPE_TOKENS.get(scope_token)
        if scope is None:
            valid = ", ".join(repr(t) for t in SELECTOR_SCOPE_TOKENS)
            raise click.ClickException(
                f"Invalid provision selector {name_selector!r}: unknown scope {scope_token!r}. Must be one of: {valid}."
            )

        matches = [h for h in all_handlers if h.scope == scope and h.name == entry_name]
        if not matches:
            raise click.ClickException(
                f"No provision handler matches selector {name_selector!r} "
                f"(no {scope.value!r}-scope entry named {entry_name!r})."
            )
        if len(matches) > 1:
            raise click.ClickException(
                f"Provision selector {name_selector!r} matches {len(matches)} handlers "
                f"({', '.join(sorted({m.source for m in matches}))}) — ambiguous. "
                f"'name' must be unique within its scope grouping across every manifest source."
            )
        return matches[0]

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
        action: ProvisionAction,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Run all handlers for *st*.

        Returns ``"aborted"`` when an apply failure should abort the remaining
        sub-targets, ``"error"`` when a destroy/reset failure ends the run
        non-ok, ``"clean_error"`` when at least one clean handler failed but
        every handler in *handlers* still ran, or ``None`` when all handlers
        succeeded.
        """
        had_clean_failure = False
        for handler in handlers:
            signal = self._run_handler_with_action(
                handler=handler,
                env_name=env_name,
                action=action,
                service_check_result=service_check_result,
                reporter=reporter,
            )
            if signal == "clean_error":
                # Best-effort: keep running the rest of this sub-target's
                # handlers instead of stopping at the first failing clean.
                had_clean_failure = True
                continue
            if signal is not None:
                return signal
        return "clean_error" if had_clean_failure else None

    def _run_handler_with_action(
        self,
        *,
        handler: ProvisionHandler,
        env_name: str,
        action: ProvisionAction,
        service_check_result: str | None,
        reporter: IProvisionReporter,
    ) -> str | None:
        """Run a single handler, resolving the action.

        Returns ``"aborted"`` for an apply failure (abort the remaining
        sub-targets), ``"error"`` for a destroy/reset failure (end the run
        non-ok without chain-abort semantics), ``"clean_error"`` for a clean
        failure (end the run non-ok without stopping any other handler or
        sub-target), or ``None`` on success.

        Dispatches exhaustively over ``ProvisionAction`` — mirroring
        ``ProvisionExecutionService._resolve_script`` and ``_action_verb`` —
        so a member added to that enum without a case here fails loudly
        (``pyright`` flags the ``assert_never``) instead of silently falling
        through to ``_run_action``'s apply branch.
        """
        scope_str = handler.scope.value
        source_str = handler.source
        st = handler.subtarget

        if action is ProvisionAction.destroy:
            return self._run_destroy(
                handler=handler,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        if action is ProvisionAction.reset:
            return self._run_reset(
                handler=handler,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        if action is ProvisionAction.clean:
            return self._run_clean(
                handler=handler,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )

        if action is ProvisionAction.apply:
            return self._run_action(
                handler=handler,
                action=action,
                env_name=env_name,
                scope_str=scope_str,
                source_str=source_str,
                st=st,
                service_check_result=service_check_result,
                reporter=reporter,
            )
        assert_never(action)

    def _run_clean(
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
        """Run clean: declared clean → run it; no declared clean → warn and no-op.

        Selection already filters a handler with no declared ``clean`` out of
        a full-chain/subtarget run, so this warn path is reached only via an
        explicit ``--name`` selector naming a handler that declares none.
        """
        if handler.clean is None:
            reporter.handler_warn(
                subtarget=st,
                scope=scope_str,
                source=source_str,
                message="no clean command declared — skipping",
            )
            return None
        return self._run_action(
            handler=handler,
            action=ProvisionAction.clean,
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
                message="no destroy command declared — skipping",
            )
            return None
        return self._run_action(
            handler=handler,
            action=ProvisionAction.destroy,
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
                action=ProvisionAction.reset,
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
                action=ProvisionAction.destroy,
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
                action=ProvisionAction.apply,
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
            message="no reset or destroy command declared — degrading to apply",
        )
        return self._run_action(
            handler=handler,
            action=ProvisionAction.apply,
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
        action: ProvisionAction,
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
        ``"clean_error"`` when a clean script exits non-zero, or ``None`` when
        the action succeeded.
        """
        result: HandlerExecutionResult = self._execution_svc.run_handler(handler, action, env_name, reporter)
        runs_json: list[dict[str, Any]] = [{"cwd": str(r.cwd), "exit_status": r.exit_code} for r in result.runs]
        overall_exit = 0 if result.ok else 1
        reporter.handler_result(
            subtarget=st,
            scope=scope_str,
            source=source_str,
            action=action.value,
            service_check=service_check_result,
            runs=runs_json,
            exit_status=overall_exit,
        )
        if not result.ok:
            if action is ProvisionAction.apply:
                # Apply failure aborts the remaining sub-targets in the chain.
                return "aborted"
            if action is ProvisionAction.clean:
                # Clean is best-effort artifact removal: a failing clean must
                # not strand the rest of the run, so this signals failure
                # without abort/chain-abort semantics — the caller keeps
                # running the remaining handlers and sub-targets.
                return "clean_error"
            # Destroy or reset failure: end the run with error status without
            # chain-abort semantics (destroy/reset target a single sub-target).
            return "error"
        return None
