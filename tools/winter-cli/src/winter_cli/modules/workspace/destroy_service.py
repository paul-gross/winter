from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.provision.provision_reporter import IProvisionReporter
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry
from winter_cli.modules.workspace.extension_hook_service import ExtensionHookService
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.internal.managed_block import (
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    strip_block,
)
from winter_cli.modules.workspace.models import ProjectRepository, RepoError
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

if TYPE_CHECKING:
    from winter_cli.modules.provision.provision_service import ProvisionService

logger = logging.getLogger(__name__)

_TEARDOWN_SUBTARGETS = ("data", "resource")


class _DestroyProvisionReporter:
    """Thin IProvisionReporter adapter that routes key events to IInitReporter.

    Used exclusively inside destroy_env so provision teardown progress is
    surfaced through the same reporter the caller already holds, without
    requiring a separate provision-reporter injection path on DestroyService.

    Emits a single bracketing provision_teardown_started/finished pair around
    the entire two-phase teardown (data → resource).  Per-subtarget and
    per-handler events are still emitted individually inside that bracket.
    """

    def __init__(self, reporter: IInitReporter, env_name: str) -> None:
        self._reporter = reporter
        self._env_name = env_name
        self._started = False

    def _ensure_started(self, subtargets: list[str]) -> None:
        if not self._started:
            self._started = True
            self._reporter.repo_action(
                self._env_name,
                self._env_name,
                "provision_teardown_started",
                " → ".join(subtargets),
            )

    # ── IProvisionOutputSink ──────────────────────────────────────────────

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self._reporter.cmd_started(label, action)

    def execution_output_line(self, label: str, line: str) -> None:
        self._reporter.cmd_output_line(label, line)

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        self._reporter.cmd_completed(label, action, exit_code)

    def execution_error(self, label: str, error: str) -> None:
        self._reporter.repo_error(label, error)

    # ── Provision-level lifecycle ─────────────────────────────────────────

    def provision_started(self, env: str, subtargets: list[str]) -> None:
        # Suppress the per-run started event; the outer bracket is emitted
        # lazily on first provision_started call via _ensure_started.
        self._ensure_started(subtargets)

    def subtarget_started(self, subtarget: str) -> None:
        self._reporter.repo_action(self._env_name, subtarget, "provision_subtarget_started")

    def no_handlers(self, subtarget: str) -> None:
        self._reporter.repo_action(self._env_name, subtarget, "provision_no_handlers")

    def handler_result(
        self,
        subtarget: str,
        scope: str,
        source: str,
        action: str,
        service_check: str | None,
        runs: list[dict[str, Any]],
        exit_status: int,
    ) -> None:
        label = f"{source}/{subtarget}[{scope}]"
        self._reporter.repo_action(self._env_name, label, "provision_handler_done", action)

    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None:
        label = f"{source}/{subtarget}[{scope}]"
        self._reporter.repo_action(self._env_name, label, "provision_handler_warn", message)

    def provision_finished(self, status: str, aborted_at: str | None) -> None:
        # Suppress per-run finished events; the outer bracket is emitted
        # once after both subtargets complete via emit_finished().
        pass

    def emit_finished(self, status: str) -> None:
        """Emit the single outer finished bracket after both subtargets complete."""
        self._reporter.repo_action(
            self._env_name,
            self._env_name,
            "provision_teardown_finished",
            status,
        )

    def plan_handler(
        self,
        subtarget: str,
        scope: str,
        source: str,
        commands: list[str],
        action: str,
        required_services: list[str],
        service_check_preview: str | None,
    ) -> None:
        label = f"{source}/{subtarget}[{scope}]"
        script = " && ".join(commands) if commands else ""
        self._reporter.repo_action(self._env_name, label, "would_provision_teardown", f"{action}: {script}")


def _conforms_destroy_provision_reporter(x: _DestroyProvisionReporter) -> IProvisionReporter:
    return x


class DestroyService:
    """Tear down a feature env: fire extension hooks, remove per-repo worktrees, drop the env dir.

    The hooks fire *before* any filesystem mutation so extensions still see
    the live env (tmux sessions to kill, watchers to stop, tunnels to close).
    Removal then walks every declared project repo and runs
    `git worktree remove`; any directory left behind under the env path is
    cleared by an `rmtree` pass at the end so stray files don't strand the
    env after a partial earlier teardown.

    Error-handling shape: `destroy_env` is the aggregator and collects per-phase
    booleans. Each per-repo and per-step helper wraps `(RepoError, OSError)`
    once at its boundary; leaves raise.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        repo_factory: RepositoryFactory,
        extension_hook_svc: ExtensionHookService,
        fs: IFilesystemWriter,
        git_repo: IGitRepository,
        registry: IEnvIndexRegistry,
        provision_svc: ProvisionService | None = None,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._extension_hook_svc = extension_hook_svc
        self._fs = fs
        self._git_repo = git_repo
        self._registry = registry
        self._provision_svc = provision_svc

    def destroy_env(
        self,
        name: str,
        force: bool,
        strict: bool,
        dry_run: bool,
        reporter: IInitReporter,
        provision_teardown: bool = True,
    ) -> bool:
        logger.info("destroy_env start: name=%s force=%s strict=%s dry_run=%s", name, force, strict, dry_run)
        reporter.target_started(name)

        env_root = self._config.workspace_root / name
        if not self._fs.is_dir(env_root):
            logger.warning("destroy_env: env directory not found at %s", env_root)
            reporter.repo_error(name, f"env directory not found at {env_root}")
            reporter.target_completed(name, False)
            return False

        project_repos = self._repo_factory.get_project_repos()
        existing_worktrees: list[tuple[ProjectRepository, Path]] = [
            (repo, env_root / repo.name) for repo in project_repos if self._fs.is_dir(env_root / repo.name)
        ]

        # Phase 1: safety check — refuse if any worktree is dirty unless --force.
        if not force:
            dirty: list[str] = []
            for repo, wt_path in existing_worktrees:
                if not self._git_repo.is_worktree_clean(wt_path):
                    dirty.append(repo.name)
            if dirty:
                logger.warning("destroy_env: refusing — dirty worktrees in %s: %s", name, ", ".join(dirty))
                reporter.repo_error(
                    name,
                    "refusing to destroy — dirty worktrees: " + ", ".join(dirty) + ". Re-run with --force to bypass.",
                )
                reporter.target_completed(name, False)
                return False

        # Phase 2: extension hooks (always fire before removal).
        standalones = self._repo_factory.get_standalone_repos()
        if dry_run:
            # Dry-run: emit provision teardown plan first (if applicable), then
            # structural plan events — no side effects.
            if provision_teardown and self._provision_svc is not None:
                prov_reporter = _DestroyProvisionReporter(reporter, name)
                for st in _TEARDOWN_SUBTARGETS:
                    self._provision_svc.run(
                        env_name=name,
                        subtarget=st,
                        reset=False,
                        destroy=True,
                        seed=False,
                        no_service_check=True,
                        reporter=_conforms_destroy_provision_reporter(prov_reporter),
                        dry_run=True,
                    )
                prov_reporter.emit_finished("ok")
            for repo, wt_path in existing_worktrees:
                reporter.repo_action(
                    repo.name,
                    str(wt_path),
                    "would_remove_worktree",
                )
            reporter.repo_action(
                name,
                str(env_root),
                "would_remove_env",
            )
            exclude_path = self._config.workspace_root / ".git" / "info" / "exclude"
            if self._self_exclude_present(env_name=name, exclude_path=exclude_path):
                reporter.repo_action(
                    name,
                    str(exclude_path),
                    "would_remove_workspace_exclude",
                    f"/{name}/",
                )
            reporter.target_completed(name, True)
            return True

        # Phase 2a: provision teardown — run data --destroy then resource --destroy
        # before extension hooks and worktree removal, so provisioned resources are
        # cleaned up while the env directory still exists on disk.
        teardown_ok = True
        if provision_teardown and self._provision_svc is not None:
            prov_reporter = _DestroyProvisionReporter(reporter, name)
            teardown_ok = self._run_provision_teardown(
                name=name,
                provision_svc=self._provision_svc,
                prov_reporter=prov_reporter,
                reporter=reporter,
                strict=strict,
            )
            if not teardown_ok and strict:
                reporter.repo_error(
                    name,
                    "aborting destroy — provision teardown failed and --strict was set",
                )
                reporter.target_completed(name, False)
                return False

        # Phase 2b: extension hooks.
        hooks_ok = self._extension_hook_svc.run_env_destroy_hooks(
            standalones,
            env_root,
            name,
            reporter,
        )
        if not hooks_ok and strict:
            reporter.repo_error(
                name,
                "aborting destroy — on_env_destroy hook failed and --strict was set",
            )
            reporter.target_completed(name, False)
            return False
        # Non-strict mode: hook errors were logged by the extension service.
        # We deliberately do not propagate them into the overall exit code,
        # matching the documented "logs an error but does not block destruction".

        # Phase 3: remove every project repo's worktree from its source checkout.
        success = True
        for repo, wt_path in existing_worktrees:
            if not self._remove_git_worktree(repo, wt_path, force, reporter):
                success = False

        # Phase 4: drop the env directory itself (covers .winter.env and any
        # stray files from project setup steps).
        if not self._remove_env_directory(name, env_root, reporter):
            success = False

        # Phase 5: strip the matching `winter-dir/<env>` block from the workspace
        # `.git/info/exclude`. Init writes this block in `_write_workspace_self_exclude`;
        # leaving it behind would orphan a stale ignore rule.
        if not self._strip_self_exclude(name, reporter):
            success = False

        # Phase 6: remove the env-index registry entry so the index can be
        # reused by a future env with the same name.
        self._registry.remove(name)

        # Propagate a non-strict teardown failure into the overall success flag.
        if not teardown_ok:
            success = False

        reporter.target_completed(name, success)
        return success

    def _run_provision_teardown(
        self,
        *,
        name: str,
        provision_svc: ProvisionService,
        prov_reporter: _DestroyProvisionReporter,
        reporter: IInitReporter,
        strict: bool,
    ) -> bool:
        """Run data --destroy then resource --destroy.

        Returns True when all subtargets succeeded, False on any failure.
        Config/ClickException errors degrade to warn+continue so a broken
        manifest doesn't strand the env on disk.
        """
        ok = True
        adapted = _conforms_destroy_provision_reporter(prov_reporter)
        for st in _TEARDOWN_SUBTARGETS:
            try:
                summary = provision_svc.run(
                    env_name=name,
                    subtarget=st,
                    reset=False,
                    destroy=True,
                    seed=False,
                    no_service_check=True,
                    reporter=adapted,
                    dry_run=False,
                )
            except click.ClickException as exc:
                # Config error or missing env — warn and continue so structural
                # teardown can still remove the env dir.
                reporter.repo_error(name, f"provision teardown ({st}) config error: {exc.format_message()}")
                ok = False
                continue

            if summary.status != "ok":
                reporter.repo_error(
                    name,
                    f"provision teardown ({st}) failed with status {summary.status!r}",
                )
                ok = False

        final_status = "ok" if ok else "error"
        prov_reporter.emit_finished(final_status)
        return ok

    def _self_exclude_present(self, env_name: str, exclude_path: Path) -> bool:
        if not self._fs.exists(exclude_path):
            return False
        try:
            content = self._fs.read_text(exclude_path)
        except OSError:
            return False
        marker = GITIGNORE_BEGIN.format(name=f"winter-dir/{env_name}")
        return marker in content

    def _remove_env_directory(self, name: str, env_root: Path, reporter: IInitReporter) -> bool:
        if not self._fs.exists(env_root):
            return True
        try:
            self._fs.rmtree(env_root)
        except OSError as exc:
            reporter.repo_error(name, f"removing env directory — {exc}")
            return False
        reporter.repo_action(name, str(env_root), "env_removed")
        return True

    def _strip_self_exclude(self, env_name: str, reporter: IInitReporter) -> bool:
        exclude_path = self._config.workspace_root / ".git" / "info" / "exclude"
        if not self._fs.exists(exclude_path):
            return True

        block_name = f"winter-dir/{env_name}"
        begin = GITIGNORE_BEGIN.format(name=block_name)
        end = GITIGNORE_END.format(name=block_name)

        try:
            existing = self._fs.read_text(exclude_path)
            new_content = strip_block(existing, begin, end)
            if new_content == existing:
                return True
            self._fs.write_text(exclude_path, new_content)
        except OSError as exc:
            reporter.repo_error(env_name, f".git/info/exclude — {exc}")
            return False

        reporter.repo_action(
            env_name,
            str(exclude_path),
            "workspace_excludes_updated",
            f"removed /{env_name}/",
        )
        return True

    def _remove_git_worktree(
        self,
        repo: ProjectRepository,
        worktree_path: Path,
        force: bool,
        reporter: IInitReporter,
    ) -> bool:
        try:
            if not self._fs.exists(repo.main_path):
                # Source checkout is gone too — fall back to a plain rmtree so we
                # don't leave the env half-removed.
                self._fs.rmtree(worktree_path)
                reporter.repo_action(repo.name, str(worktree_path), "worktree_removed", "no source checkout")
                return True

            self._git_repo.remove_worktree(repo.main_path, worktree_path, force)
        except (RepoError, OSError) as exc:
            reporter.repo_error(repo.name, str(exc))
            return False
        reporter.repo_action(repo.name, str(worktree_path), "worktree_removed")
        return True
