from __future__ import annotations

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import git

from winter_cli.config.models import WorkspaceConfig
from winter_cli.modules.workspace.extensions import ExtensionService
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.internal.managed_block import (
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    replace_or_append_block,
)
from winter_cli.modules.workspace.internal.read_workspace_repository import resolve_env_index
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import ProjectRepository, IWorkspaceRepository, StandaloneRepository
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
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        repo_factory: RepositoryFactory,
        extension_svc: ExtensionService,
        error_factory: RepoErrorFactory,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._extension_svc = extension_svc
        self._error_factory = error_factory

    # ── Public API ────────────────────────────────────────────────────────

    def reconcile_projects(self, reporter: IInitReporter) -> bool:
        target = "projects/"
        reporter.target_started(target)

        projects_dir = self._config.workspace_root / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        success = self._write_workspace_self_exclude("projects", reporter)

        repos = self._repo_factory.get_project_repos()
        if not self._run_per_repo(
            repos, lambda r: self._reconcile_source_checkout(r, reporter)
        ):
            success = False

        reporter.target_completed(target, success)
        return success

    def reconcile_standalones(self, reporter: IInitReporter) -> bool:
        repos = self._repo_factory.get_standalone_repos()
        if not repos:
            return True

        target = "standalone/"
        reporter.target_started(target)

        success = self._run_per_repo(
            repos, lambda r: self._reconcile_standalone(r, reporter)
        )

        # Aggregate-update workspace CLAUDE.md and `.git/info/exclude` from all
        # standalones that were successfully reconciled (i.e. exist on disk now).
        # Per-repo reconcile may have failed for some, but cloned-and-present
        # extensions still belong in both managed sections.
        present_repos = [r for r in repos if r.path.exists()]
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
        env_root.mkdir(parents=True, exist_ok=True)

        if not self._write_workspace_self_exclude(name, reporter):
            success = False

        ready_repos = []
        for repo in self._repo_factory.get_project_repos():
            if not repo.main_path.exists():
                reporter.repo_error(
                    repo.name,
                    f"source checkout missing at {repo.main_path}. "
                    f"Run `winter ws init` first.",
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
            standalones, env_root, name, reporter,
        ):
            success = False

        reporter.target_completed(name, success)
        return success

    def _run_per_repo(self, repos, work_fn) -> bool:
        """Fan work_fn(repo) out across a thread pool — clones and cmds run in parallel.

        Each repo's work runs serially within its own task (clone → identity → excludes →
        cmds), but the tasks run concurrently across repos. Reporter calls are
        thread-safe via internal locking.
        """
        if not repos:
            return True
        success = True
        with ThreadPoolExecutor(max_workers=len(repos)) as pool:
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
        if not first.main_path.exists():
            return []

        try:
            r = git.Repo(str(first.main_path))
            lines = r.git.worktree("list", "--porcelain").splitlines()
        except git.GitCommandError as exc:
            # If the source checkout's worktree list is unreadable, we can't
            # know what envs exist — reconciling a subset would silently
            # leave drift, so surface this as a hard failure of `winter ws init`.
            raise self._error_factory.from_git(
                exc,
                message=f"discovering existing worktrees failed for {first.name}",
                cwd=first.main_path,
            ) from exc

        names: list[str] = []
        source_main = first.main_path.resolve()
        for line in lines:
            if not line.startswith("worktree "):
                continue
            worktree_path = Path(line[len("worktree "):]).resolve()
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

        if not repo_path.exists():
            if not repo.url:
                reporter.repo_error(label, "no `url` declared in config; cannot clone")
                return False
            if not self._clone(repo.url, repo.name, repo_path, reporter):
                return False
            reporter.repo_action(label, str(repo_path), "cloned")
        else:
            reporter.repo_action(label, str(repo_path), "exists")

        if not self._apply_identity(repo_path, reporter, label):
            return False
        if not self._write_excludes(repo_path, repo, reporter, str(repo_path)):
            return False
        if not self._run_cmds(repo_path, repo, reporter):
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

        if not repo_path.exists():
            if not repo.url:
                reporter.repo_error(label, "no `url` declared in config; cannot clone")
                return False
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._clone(repo.url, repo.name, repo_path, reporter):
                return False
            reporter.repo_action(label, str(repo_path), "cloned")
        else:
            reporter.repo_action(label, str(repo_path), "exists")

        if not self._apply_identity(repo_path, reporter, label):
            return False
        if not self._write_excludes(repo_path, repo, reporter, str(repo_path)):
            return False
        if not self._run_cmds(repo_path, repo, reporter):
            return False

        if not self._extension_svc.process(repo, reporter):
            return False
        return True

    def _clone(
        self,
        url: str,
        name: str,
        repo_path: Path,
        reporter: IInitReporter,
    ) -> bool:
        try:
            git.Repo.clone_from(url, str(repo_path))
            return True
        except git.GitCommandError as exc:
            reporter.repo_error(name, f"clone failed — {exc}")
            return False

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

        if not worktree_path.exists():
            if not self._create_git_worktree(repo, branch_name, worktree_path, reporter):
                return False
            reporter.repo_action(label, location, "worktree_created")
        else:
            reporter.repo_action(label, location, "exists")

        if not self._apply_identity(worktree_path, reporter, label):
            return False
        if not self._write_excludes(worktree_path, repo, reporter, location):
            return False
        if not self._configure_pinned_tracking(repo, worktree_path, reporter):
            return False
        if not self._run_cmds(worktree_path, repo, reporter):
            return False
        return True

    def _configure_pinned_tracking(
        self,
        repo: ProjectRepository,
        worktree_path: Path,
        reporter: IInitReporter,
    ) -> bool:
        """Wire a pinned worktree to push and pull against `origin/<main-branch>`.

        `winter ws connect` deliberately skips pinned repos — they never participate
        in feature-branch flow — so init is the only place that installs their
        upstream tracking. Pinned repos are owned by the workspace user and
        commits land directly on the main branch, so we also set
        `push.default=upstream` to make `git push` from the worktree branch
        target the main branch. Idempotent: a no-op when both are already in place.
        """
        if not repo.pinned:
            return True
        desired = f"origin/{repo.main_branch}"
        changes: list[str] = []
        try:
            r = git.Repo(str(worktree_path))

            try:
                current = r.active_branch.tracking_branch()
            except TypeError:
                current = None
            if current is None or current.name != desired:
                r.git.branch("--set-upstream-to", desired)
                changes.append(desired)

            with r.config_writer() as cw:
                if cw.get_value("push", "default", "") != "upstream":
                    cw.set_value("push", "default", "upstream")
                    changes.append("push.default=upstream")
        except (git.InvalidGitRepositoryError, git.NoSuchPathError, git.GitCommandError) as exc:
            reporter.repo_error(repo.name, f"configure pinned tracking — {exc}")
            return False

        if changes:
            reporter.repo_action(
                repo.name, str(worktree_path), "pinned_tracking_set", ", ".join(changes),
            )
        return True

    def _create_git_worktree(
        self,
        repo: ProjectRepository,
        branch_name: str,
        worktree_path: Path,
        reporter: IInitReporter,
    ) -> bool:
        try:
            source = git.Repo(str(repo.main_path))
            # If branch already exists locally, just attach the worktree to it.
            existing_heads = {h.name for h in source.heads}
            if branch_name in existing_heads:
                source.git.worktree("add", str(worktree_path), branch_name)
            else:
                source.git.worktree(
                    "add", str(worktree_path), "-b", branch_name, repo.main_branch,
                )
            return True
        except git.GitCommandError as exc:
            reporter.repo_error(repo.name, f"git worktree add failed — {exc}")
            return False

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
        if not (self._config.workspace_root / ".git").exists():
            return True

        existing = ""
        if exclude_path.exists():
            try:
                existing = exclude_path.read_text()
            except OSError as exc:
                reporter.repo_error("winter", f"reading .git/info/exclude — {exc}")
                return False

        new_content = replace_or_append_block(existing, begin, end, desired_lines)
        if new_content == existing:
            return True

        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            exclude_path.write_text(new_content)
        except OSError as exc:
            reporter.repo_error("winter", f"writing .git/info/exclude — {exc}")
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
        existing = ""
        if env_path.exists():
            try:
                existing = env_path.read_text()
            except OSError as exc:
                reporter.repo_error("winter", f"reading {WINTER_ENV_FILE} — {exc}")
                return False

        new_content = self._replace_or_prepend_block(
            existing, WINTER_ENV_BEGIN, WINTER_ENV_END, block_lines,
        )
        if new_content == existing:
            return True

        try:
            env_path.write_text(new_content)
        except OSError as exc:
            reporter.repo_error("winter", f"writing {WINTER_ENV_FILE} — {exc}")
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
            new_lines = lines[:begin_idx] + desired_lines + lines[end_idx + 1:]
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

    def _apply_identity(
        self,
        repo_path: Path,
        reporter: IInitReporter,
        repo_name: str,
    ) -> bool:
        identity = self._config.git_identity
        if identity is None:
            return True
        try:
            r = git.Repo(str(repo_path))
            with r.config_writer(config_level="repository") as cw:
                cw.set_value("user", "name", identity.name)
                cw.set_value("user", "email", identity.email)
            return True
        except (git.InvalidGitRepositoryError, git.NoSuchPathError, OSError) as exc:
            reporter.repo_error(repo_name, f"git identity — {exc}")
            return False

    def _write_excludes(
        self,
        repo_path: Path,
        repo: IWorkspaceRepository,
        reporter: IInitReporter,
        location: str,
    ) -> bool:
        entries = list(self._config.git_excludes) + list(repo.git_excludes)
        if not entries:
            return True

        exclude_path = self._exclude_path(repo_path)
        if exclude_path is None:
            reporter.repo_error(
                repo.name,
                f"could not locate .git/info/exclude at {repo_path}",
            )
            return False

        existing: list[str] = []
        if exclude_path.exists():
            existing = exclude_path.read_text().splitlines()

        existing_set = {line.strip() for line in existing if line.strip()}
        appended: list[str] = []
        for entry in entries:
            entry = entry.strip()
            if not entry or entry in existing_set:
                continue
            appended.append(entry)
            existing_set.add(entry)

        if not appended:
            return True

        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a") as f:
            if existing and not existing[-1].endswith("\n"):
                f.write("\n")
            f.write("# winter-managed\n")
            for entry in appended:
                f.write(entry + "\n")

        reporter.repo_action(
            repo.name, location, "excludes_updated", ", ".join(appended)
        )
        return True

    @staticmethod
    def _exclude_path(repo_path: Path) -> Path | None:
        """Locate .git/info/exclude, following the `.git` file pointer used by worktrees."""
        git_dir = repo_path / ".git"
        if git_dir.is_dir():
            return git_dir / "info" / "exclude"
        if git_dir.is_file():
            contents = git_dir.read_text().strip()
            if contents.startswith("gitdir:"):
                resolved = Path(contents.split(":", 1)[1].strip())
                if not resolved.is_absolute():
                    resolved = (repo_path / resolved).resolve()
                # For worktrees, common info/ lives under the main .git directory.
                common_dir = resolved / "commondir"
                if common_dir.is_file():
                    common_rel = common_dir.read_text().strip()
                    main_git = (resolved / common_rel).resolve()
                    return main_git / "info" / "exclude"
                return resolved / "info" / "exclude"
        return None

    def _run_cmds(
        self,
        repo_path: Path,
        repo: IWorkspaceRepository,
        reporter: IInitReporter,
    ) -> bool:
        if not repo.cmd:
            return True
        env = os.environ.copy()
        env.update(TUI_SUPPRESS_ENV)
        for command in repo.cmd:
            reporter.cmd_started(repo.name, command)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(repo_path),
                    shell=True,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                reporter.repo_error(repo.name, f"`{command}` — {exc}")
                return False
            assert proc.stdout is not None
            for line in proc.stdout:
                reporter.cmd_output_line(repo.name, line.rstrip("\n"))
            returncode = proc.wait()
            reporter.cmd_completed(repo.name, command, returncode)
            if returncode != 0:
                reporter.repo_error(
                    repo.name,
                    f"`{command}` exited with code {returncode}",
                )
                return False
        return True
