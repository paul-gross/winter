from __future__ import annotations

import logging
import os
from concurrent.futures import as_completed
from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.internal.git_ops_service import GitOpsService
from winter_cli.modules.workspace.internal.managed_block import (
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    replace_or_append_block,
)
from winter_cli.modules.workspace.internal.read_workspace_repository import resolve_env_index
from winter_cli.modules.workspace.models import (
    IWorkspaceRepository,
    ProjectRepository,
    RepoError,
    StandaloneRepository,
)
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

logger = logging.getLogger(__name__)

WINTER_ENV_FILE = ".winter.env"
WINTER_ENV_BEGIN = "# >>> winter (managed) — base environment variables; do not edit by hand"
WINTER_ENV_END = "# <<< winter (managed) — project-specific variables go below this marker"
PORT_BASE = 4000
PORT_STEP = 100

TUI_SUPPRESS_ENV = {
    "CI": "1",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "NPM_CONFIG_PROGRESS": "false",
    "PNPM_PROGRESS": "false",
    "PYTHONUNBUFFERED": "1",
    "PIP_PROGRESS_BAR": "off",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "MISE_QUIET": "true",
}


class InitService:
    """Idempotent reconcile for source checkouts, standalone repos, and feature worktrees.

    Every operation is designed to be a no-op when the target already matches the
    config, so repeated runs are safe. Each run reapplies git identity, git-exclude
    entries, and setup commands — these are the knobs that drift most often between
    config edits.

    Error-handling shape: each per-repo task and each workspace-level step has one
    wrap-site that catches `(RepoError, OSError)` and routes the failure through
    the reporter. Leaves either return their result or raise. The public reconcile
    entrypoints are aggregators — they collect per-step booleans and never `try`.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        repo_factory: RepositoryFactory,
        extension_svc: ExtensionService,
        fs: IFilesystemWriter,
        subprocess_runner: ISubprocessRunner,
        git_repo: IGitRepository,
        git_ops: GitOpsService,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._extension_svc = extension_svc
        self._fs = fs
        self._subprocess = subprocess_runner
        self._git_repo = git_repo
        self._git_ops = git_ops

    # ── Public API ────────────────────────────────────────────────────────

    def reconcile_projects(self, reporter: IInitReporter) -> bool:
        target = "projects/"
        reporter.target_started(target)

        projects_dir = self._config.workspace_root / "projects"
        self._fs.mkdir(projects_dir, parents=True, exist_ok=True)

        success = self._write_workspace_self_exclude("projects", reporter)

        repos = self._repo_factory.get_project_repos()
        if not self._run_per_repo(repos, lambda r: self._reconcile_source_checkout(r, reporter)):
            success = False

        reporter.target_completed(target, success)
        return success

    def reconcile_standalones(self, reporter: IInitReporter) -> bool:
        repos = self._repo_factory.get_standalone_repos()
        if not repos:
            return True

        target = "standalone/"
        reporter.target_started(target)

        success = self._run_per_repo(repos, lambda r: self._reconcile_standalone(r, reporter))

        # Aggregate-update workspace CLAUDE.md and `.git/info/exclude` from all
        # standalones that were successfully reconciled (i.e. exist on disk now).
        # Per-repo reconcile may have failed for some, but cloned-and-present
        # extensions still belong in both managed sections.
        present_repos = [r for r in repos if self._fs.exists(r.path)]
        if not self._extension_svc.finalize_claudemd(present_repos, reporter):
            success = False
        if not self._extension_svc.finalize_excludes(present_repos, reporter):
            success = False

        reporter.target_completed(target, success)
        return success

    def reconcile_env(self, name: str, reporter: IInitReporter) -> bool:
        reporter.target_started(name)
        success = True

        env_root = self._config.workspace_root / name
        self._fs.mkdir(env_root, parents=True, exist_ok=True)

        if not self._write_workspace_self_exclude(name, reporter):
            success = False

        ready_repos = []
        for repo in self._repo_factory.get_project_repos():
            if not self._fs.exists(repo.main_path):
                reporter.repo_error(
                    repo.name,
                    f"source checkout missing at {repo.main_path}. Run `winter ws init` first.",
                )
                success = False
                continue
            ready_repos.append(repo)

        if not self._run_per_repo(
            ready_repos,
            lambda r: self._reconcile_worktree_repo(r, name, env_root, reporter),
        ):
            success = False

        if not self._seed_winter_env(env_root, name, reporter):
            success = False

        standalones = self._repo_factory.get_standalone_repos()
        if not self._extension_svc.run_env_init_hooks(
            standalones,
            env_root,
            name,
            reporter,
        ):
            success = False

        reporter.target_completed(name, success)
        return success

    def _run_per_repo(self, repos, work_fn) -> bool:
        """Fan work_fn(repo) out across the shared GitOpsService thread pool.

        Each repo's work runs serially within its own task (clone → identity → excludes →
        cmds), but the tasks run concurrently across repos. The pool is capped at
        `GitOpsService.PARALLELISM` so a workspace with many repos doesn't overwhelm
        the SSH connection limit on remote git hosts (Codeberg in particular). Reporter
        calls are thread-safe via internal locking.
        """
        if not repos:
            return True
        success = True
        with self._git_ops.executor() as pool:
            futures = [pool.submit(work_fn, repo) for repo in repos]
            for fut in as_completed(futures):
                if not fut.result():
                    success = False
        return success

    def reconcile_all(self, reporter: IInitReporter) -> bool:
        success = self.reconcile_projects(reporter)
        if not self.reconcile_standalones(reporter):
            success = False
        for name in self._discover_existing_worktrees():
            if not self.reconcile_env(name, reporter):
                success = False
        return success

    def _discover_existing_worktrees(self) -> list[str]:
        """Use `git worktree list` on the first declared repo to find every existing worktree.

        Git knows exactly where its worktrees live, which is cheaper and more authoritative
        than scanning the filesystem. The source checkout itself is filtered out.
        """
        project_repos = self._repo_factory.get_project_repos()
        if not project_repos:
            return []

        first = project_repos[0]
        if not self._fs.exists(first.main_path):
            return []

        worktree_paths = self._git_repo.list_worktrees(first.main_path)

        names: list[str] = []
        source_main = first.main_path.resolve()
        for raw in worktree_paths:
            worktree_path = raw.resolve()
            if worktree_path == source_main:
                continue
            # worktree path is <workspace>/<name>/<repo_name>; we want <name>
            parent = worktree_path.parent
            if parent.parent.resolve() != self._config.workspace_root.resolve():
                continue
            names.append(parent.name)
        return sorted(set(names))

    # ── Source checkout ───────────────────────────────────────────────────

    def _reconcile_source_checkout(
        self,
        repo: ProjectRepository,
        reporter: IInitReporter,
    ) -> bool:
        repo_path = repo.main_path
        label = repo.name

        try:
            if not self._fs.exists(repo_path):
                if not repo.url:
                    raise RepoError("no `url` declared in config; cannot clone")
                self._git_repo.clone(repo.url, repo_path)
                reporter.repo_action(label, str(repo_path), "cloned")
            else:
                reporter.repo_action(label, str(repo_path), "exists")

            self._apply_identity(repo_path)
            self._write_excludes(repo_path, repo, reporter, str(repo_path))
            self._run_cmds(repo_path, repo, reporter)
        except (RepoError, OSError) as exc:
            reporter.repo_error(label, str(exc))
            return False
        return True

    # ── Standalone repo ───────────────────────────────────────────────────

    def _reconcile_standalone(
        self,
        repo: StandaloneRepository,
        reporter: IInitReporter,
    ) -> bool:
        repo_path = repo.path
        label = repo.name

        try:
            if not self._fs.exists(repo_path):
                if not repo.url:
                    raise RepoError("no `url` declared in config; cannot clone")
                self._fs.mkdir(repo_path.parent, parents=True, exist_ok=True)
                self._git_repo.clone(repo.url, repo_path)
                reporter.repo_action(label, str(repo_path), "cloned")
            else:
                reporter.repo_action(label, str(repo_path), "exists")

            self._apply_identity(repo_path)
            self._write_excludes(repo_path, repo, reporter, str(repo_path))
            self._run_cmds(repo_path, repo, reporter)
        except (RepoError, OSError) as exc:
            reporter.repo_error(label, str(exc))
            return False

        return self._extension_svc.process(repo, reporter)

    # ── Feature worktree ──────────────────────────────────────────────────

    def _reconcile_worktree_repo(
        self,
        repo: ProjectRepository,
        branch_name: str,
        env_root: Path,
        reporter: IInitReporter,
    ) -> bool:
        worktree_path = env_root / repo.name
        location = str(worktree_path)
        label = repo.name

        try:
            if not self._fs.exists(worktree_path):
                self._create_git_worktree(repo, branch_name, worktree_path)
                reporter.repo_action(label, location, "worktree_created")
            else:
                reporter.repo_action(label, location, "exists")

            self._apply_identity(worktree_path)
            self._write_excludes(worktree_path, repo, reporter, location)
            self._configure_pinned_tracking(repo, worktree_path, reporter)
            self._run_cmds(worktree_path, repo, reporter)
        except (RepoError, OSError) as exc:
            reporter.repo_error(label, str(exc))
            return False
        return True

    def _configure_pinned_tracking(
        self,
        repo: ProjectRepository,
        worktree_path: Path,
        reporter: IInitReporter,
    ) -> None:
        """Wire a pinned worktree to push and pull against `origin/<main-branch>`.

        `winter ws connect` deliberately skips pinned repos — they never participate
        in feature-branch flow — so init is the only place that installs their
        upstream tracking. Pinned repos are owned by the workspace user and
        commits land directly on the main branch, so we also set
        `push.default=upstream` to make `git push` from the worktree branch
        target the main branch. Idempotent: a no-op when both are already in place.
        """
        if not repo.pinned:
            return
        desired = f"origin/{repo.main_branch}"
        changes: list[str] = []
        current = self._git_repo.get_tracking_branch(worktree_path)
        if current != desired:
            self._git_repo.set_upstream_to(worktree_path, desired)
            changes.append(desired)

        if self._git_repo.get_push_default(worktree_path) != "upstream":
            self._git_repo.set_push_default_upstream(worktree_path)
            changes.append("push.default=upstream")

        if changes:
            reporter.repo_action(
                repo.name,
                str(worktree_path),
                "pinned_tracking_set",
                ", ".join(changes),
            )

    def _create_git_worktree(
        self,
        repo: ProjectRepository,
        branch_name: str,
        worktree_path: Path,
    ) -> None:
        existing_heads = set(self._git_repo.get_local_branches(repo.main_path))
        if branch_name in existing_heads:
            self._git_repo.add_worktree(repo.main_path, worktree_path, branch_name)
        else:
            self._git_repo.add_worktree(repo.main_path, worktree_path, branch_name, base_branch=repo.main_branch)

    def _write_workspace_self_exclude(
        self,
        dir_name: str,
        reporter: IInitReporter,
    ) -> bool:
        """Add `/{dir_name}/` to a managed block in the workspace's `.git/info/exclude`.

        The block is namespaced as `winter-dir/{dir_name}` so the orphan-stripping
        pass in ExtensionService.finalize_excludes leaves it alone (its regex
        rejects names containing `/`).

        Silent no-op when the workspace isn't a git repo (`.git/info/` missing
        and not creatable). Returns False only on a real I/O error.
        """
        block_name = f"winter-dir/{dir_name}"
        begin = GITIGNORE_BEGIN.format(name=block_name)
        end = GITIGNORE_END.format(name=block_name)
        desired_lines = [begin, f"/{dir_name}/", end]

        exclude_path = self._config.workspace_root / ".git" / "info" / "exclude"
        if not self._fs.exists(self._config.workspace_root / ".git"):
            return True

        try:
            existing = self._fs.read_text(exclude_path) if self._fs.exists(exclude_path) else ""
            new_content = replace_or_append_block(existing, begin, end, desired_lines)
            if new_content == existing:
                return True
            self._fs.mkdir(exclude_path.parent, parents=True, exist_ok=True)
            self._fs.write_text(exclude_path, new_content)
        except OSError as exc:
            reporter.repo_error("winter", f".git/info/exclude — {exc}")
            return False

        reporter.repo_action(
            "winter",
            str(exclude_path),
            "workspace_excludes_updated",
            f"/{dir_name}/",
        )
        return True

    def _seed_winter_env(
        self,
        env_root: Path,
        env_name: str,
        reporter: IInitReporter,
    ) -> bool:
        """Seed the worktree's .winter.env with workspace-managed base variables.

        Writes a marker-bracketed block at the top of the file containing the
        environment's identity and port window. Project-specific variables
        (set by the project's project-setup.md) live below the closing marker
        and are preserved across re-runs. The block itself is rewritten in full
        each time, so changing the worktree's index updates the file cleanly.
        """
        index = resolve_env_index(env_name)
        port_base = PORT_BASE + index * PORT_STEP

        block_lines = [
            WINTER_ENV_BEGIN,
            f"WINTER_ENV={env_name}",
            f"WINTER_ENV_INDEX={index}",
            f"WINTER_PORT_BASE={port_base}",
            WINTER_ENV_END,
        ]

        env_path = env_root / WINTER_ENV_FILE
        try:
            existing = self._fs.read_text(env_path) if self._fs.exists(env_path) else ""
            new_content = self._replace_or_prepend_block(
                existing,
                WINTER_ENV_BEGIN,
                WINTER_ENV_END,
                block_lines,
            )
            if new_content == existing:
                return True
            self._fs.write_text(env_path, new_content)
        except OSError as exc:
            reporter.repo_error("winter", f"{WINTER_ENV_FILE} — {exc}")
            return False

        reporter.repo_action(
            "winter",
            str(env_path),
            "winter_env_seeded",
            f"WINTER_PORT_BASE={port_base}",
        )
        return True

    @staticmethod
    def _replace_or_prepend_block(
        content: str,
        begin: str,
        end: str,
        desired_lines: list[str],
    ) -> str:
        """Replace a marker-bracketed block, or prepend if not present."""
        lines = content.split("\n") if content else []
        try:
            begin_idx = lines.index(begin)
        except ValueError:
            begin_idx = -1

        if begin_idx >= 0:
            try:
                end_offset = lines[begin_idx:].index(end)
            except ValueError:
                end_idx = len(lines) - 1
            else:
                end_idx = begin_idx + end_offset
            new_lines = lines[:begin_idx] + desired_lines + lines[end_idx + 1 :]
        else:
            new_lines = list(desired_lines)
            if lines:
                # Separate the managed block from existing content with a blank line.
                if lines[0].strip() != "":
                    new_lines.append("")
                new_lines.extend(lines)

        result = "\n".join(new_lines)
        if not result.endswith("\n"):
            result += "\n"
        return result

    # ── Shared reconcile steps ────────────────────────────────────────────

    def _apply_identity(self, repo_path: Path) -> None:
        identity = self._config.git_identity
        if identity is None:
            return
        self._git_repo.set_user_identity(repo_path, identity.name, identity.email)

    def _write_excludes(
        self,
        repo_path: Path,
        repo: IWorkspaceRepository,
        reporter: IInitReporter,
        location: str,
    ) -> None:
        entries = list(self._config.git_excludes) + list(repo.git_excludes)
        if not entries:
            return

        exclude_path = self._exclude_path(repo_path)
        if exclude_path is None:
            raise RepoError(f"could not locate .git/info/exclude at {repo_path}")

        existing: list[str] = []
        if self._fs.exists(exclude_path):
            existing = self._fs.read_text(exclude_path).splitlines()

        existing_set = {line.strip() for line in existing if line.strip()}
        appended: list[str] = []
        for entry in entries:
            entry = entry.strip()
            if not entry or entry in existing_set:
                continue
            appended.append(entry)
            existing_set.add(entry)

        if not appended:
            return

        self._fs.mkdir(exclude_path.parent, parents=True, exist_ok=True)
        new_lines: list[str] = []
        if existing and not existing[-1].endswith("\n"):
            new_lines.append("")  # ensure separator before our marker
        new_lines.append("# winter-managed")
        new_lines.extend(appended)
        self._fs.append_lines(exclude_path, new_lines)

        reporter.repo_action(repo.name, location, "excludes_updated", ", ".join(appended))

    def _exclude_path(self, repo_path: Path) -> Path | None:
        """Locate .git/info/exclude, following the `.git` file pointer used by worktrees."""
        git_dir = repo_path / ".git"
        if self._fs.is_dir(git_dir):
            return git_dir / "info" / "exclude"
        if self._fs.is_file(git_dir):
            contents = self._fs.read_text(git_dir).strip()
            if contents.startswith("gitdir:"):
                resolved = Path(contents.split(":", 1)[1].strip())
                if not resolved.is_absolute():
                    resolved = (repo_path / resolved).resolve()
                # For worktrees, common info/ lives under the main .git directory.
                common_dir = resolved / "commondir"
                if self._fs.is_file(common_dir):
                    common_rel = self._fs.read_text(common_dir).strip()
                    main_git = (resolved / common_rel).resolve()
                    return main_git / "info" / "exclude"
                return resolved / "info" / "exclude"
        return None

    def _run_cmds(
        self,
        repo_path: Path,
        repo: IWorkspaceRepository,
        reporter: IInitReporter,
    ) -> None:
        if not repo.cmd:
            return
        env = os.environ.copy()
        env.update(TUI_SUPPRESS_ENV)
        for command in repo.cmd:
            reporter.cmd_started(repo.name, command)
            try:
                with self._subprocess.popen(command, cwd=repo_path, env=env, shell=True) as proc:
                    for line in proc.stdout_lines:
                        reporter.cmd_output_line(repo.name, line)
                    returncode = proc.wait()
            except OSError as exc:
                raise RepoError(f"`{command}` — {exc}") from exc
            reporter.cmd_completed(repo.name, command, returncode)
            if returncode != 0:
                raise RepoError(f"`{command}` exited with code {returncode}")
