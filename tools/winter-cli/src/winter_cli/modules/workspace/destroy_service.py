from __future__ import annotations

from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.internal.managed_block import (
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    strip_block,
)
from winter_cli.modules.workspace.models import ProjectRepository, RepoError
from winter_cli.modules.workspace.repository_factory import RepositoryFactory


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
        extension_svc: ExtensionService,
        fs: IFilesystemWriter,
        git_repo: IGitRepository,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._extension_svc = extension_svc
        self._fs = fs
        self._git_repo = git_repo

    def destroy_env(
        self,
        name: str,
        force: bool,
        strict: bool,
        dry_run: bool,
        reporter: IInitReporter,
    ) -> bool:
        reporter.target_started(name)

        env_root = self._config.workspace_root / name
        if not self._fs.is_dir(env_root):
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
                reporter.repo_error(
                    name,
                    "refusing to destroy — dirty worktrees: " + ", ".join(dirty) + ". Re-run with --force to bypass.",
                )
                reporter.target_completed(name, False)
                return False

        # Phase 2: extension hooks (always fire before removal).
        standalones = self._repo_factory.get_standalone_repos()
        if dry_run:
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

        hooks_ok = self._extension_svc.run_env_destroy_hooks(
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

        reporter.target_completed(name, success)
        return success

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
