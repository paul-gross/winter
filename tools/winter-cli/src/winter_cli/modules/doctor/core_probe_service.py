from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.config_file import ConfigFileReadError, IConfigFileReader
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.doctor.models import ProbeResult, ProbeStatus
from winter_cli.modules.workspace.models import (
    FeatureEnvironment,
    ProjectRepository,
    RepoError,
)
from winter_cli.modules.workspace.repo_repository import IWriteRepoRepository
from winter_cli.modules.workspace.repository_factory import RepositoryFactory
from winter_cli.modules.workspace.workspace_repository import IReadWorkspaceRepository

logger = logging.getLogger(__name__)

CORE_SOURCE = "core"

# Floors chosen to match what every supported workflow exercises.
MIN_GIT_VERSION = (2, 20)
MIN_PYTHON_VERSION = (3, 11)

_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)")


class CoreProbeService:
    """Built-in preflight probes for git, python, config, and on-disk repo state."""

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemReader,
        subprocess_runner: ISubprocessRunner,
        config_file_reader: IConfigFileReader,
        repo_factory: RepositoryFactory,
        worktree_repo: IReadWorkspaceRepository,
        repo_repo: IWriteRepoRepository,
    ) -> None:
        self._config = config
        self._fs = fs
        self._subprocess = subprocess_runner
        self._config_file_reader = config_file_reader
        self._repo_factory = repo_factory
        self._worktree_repo = worktree_repo
        self._repo_repo = repo_repo

    def run(self) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        results.append(self._probe_git())
        results.append(self._probe_python())
        results.append(self._probe_config_parses())
        project_repos = self._repo_factory.get_project_repos()
        results.extend(self._probe_project_repos(project_repos))
        results.extend(self._probe_standalone_repos())
        results.extend(self._probe_envs(project_repos))
        claude_symlinks = self._probe_claude_symlinks()
        if claude_symlinks is not None:
            results.append(claude_symlinks)
        return results

    # ── git ───────────────────────────────────────────────────────────────

    def _probe_git(self) -> ProbeResult:
        try:
            result = self._subprocess.run(["git", "--version"])
        except FileNotFoundError:
            return ProbeResult(
                source=CORE_SOURCE,
                name="git binary",
                status=ProbeStatus.fail,
                message="git not found on PATH",
                remediation="Install git (e.g. `dnf install git`, `brew install git`).",
            )
        except OSError as exc:
            return ProbeResult(
                source=CORE_SOURCE,
                name="git binary",
                status=ProbeStatus.fail,
                message=f"failed to invoke git: {exc}",
            )
        if result.returncode != 0:
            return ProbeResult(
                source=CORE_SOURCE,
                name="git binary",
                status=ProbeStatus.fail,
                message=result.stderr.strip() or "git exited non-zero",
            )
        match = _GIT_VERSION_RE.search(result.stdout)
        if not match:
            return ProbeResult(
                source=CORE_SOURCE,
                name="git binary",
                status=ProbeStatus.warn,
                message=f"unrecognized version string: {result.stdout.strip()}",
            )
        version = (int(match.group(1)), int(match.group(2)))
        version_str = f"{version[0]}.{version[1]}"
        if version < MIN_GIT_VERSION:
            min_str = f"{MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]}"
            return ProbeResult(
                source=CORE_SOURCE,
                name="git binary",
                status=ProbeStatus.warn,
                message=f"git {version_str} (recommend >= {min_str})",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name="git binary",
            status=ProbeStatus.pass_,
            message=f"git {version_str}",
        )

    # ── python ────────────────────────────────────────────────────────────

    def _probe_python(self) -> ProbeResult:
        current = sys.version_info[:2]
        current_str = f"{current[0]}.{current[1]}"
        min_str = f"{MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}"
        if current < MIN_PYTHON_VERSION:
            return ProbeResult(
                source=CORE_SOURCE,
                name="python version",
                status=ProbeStatus.fail,
                message=f"python {current_str} (require >= {min_str})",
                remediation=f"Use python >= {min_str} (see tools/winter-cli/pyproject.toml).",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name="python version",
            status=ProbeStatus.pass_,
            message=f"python {current_str}",
        )

    # ── config parse ──────────────────────────────────────────────────────

    def _probe_config_parses(self) -> ProbeResult:
        config_path = self._config.workspace_root / ".winter" / "config.toml"
        if not self._fs.is_file(config_path):
            return ProbeResult(
                source=CORE_SOURCE,
                name=".winter/config.toml",
                status=ProbeStatus.fail,
                message=f"missing at {config_path}",
                remediation="Create .winter/config.toml or run from a workspace root.",
            )
        try:
            self._config_file_reader.load(config_path)
        except ConfigFileReadError as exc:
            return ProbeResult(
                source=CORE_SOURCE,
                name=".winter/config.toml",
                status=ProbeStatus.fail,
                message=str(exc),
                remediation="Fix the TOML syntax in .winter/config.toml.",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name=".winter/config.toml",
            status=ProbeStatus.pass_,
            message="parses",
        )

    # ── project + standalone repo presence ────────────────────────────────

    def _probe_project_repos(self, project_repos: list[ProjectRepository]) -> list[ProbeResult]:
        return [self._probe_repo_dir("project repo", r.name, r.main_path) for r in project_repos]

    def _probe_standalone_repos(self) -> list[ProbeResult]:
        repos = self._repo_factory.get_standalone_repos()
        return [self._probe_repo_dir("standalone repo", r.name, r.path) for r in repos]

    def _probe_repo_dir(self, kind: str, name: str, path: Path) -> ProbeResult:
        label = f"{kind}: {name}"
        if not self._fs.is_dir(path):
            return ProbeResult(
                source=CORE_SOURCE,
                name=label,
                status=ProbeStatus.fail,
                message=f"missing directory {path}",
                remediation="Run `winter ws init` to clone declared repos.",
            )
        if not self._fs.exists(path / ".git"):
            return ProbeResult(
                source=CORE_SOURCE,
                name=label,
                status=ProbeStatus.fail,
                message=f"not a git repository ({path})",
                remediation="Remove the stray directory and re-run `winter ws init`.",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name=label,
            status=ProbeStatus.pass_,
            message=str(path),
        )

    # ── env consistency ───────────────────────────────────────────────────

    def _probe_envs(self, project_repos: list[ProjectRepository]) -> list[ProbeResult]:
        try:
            workspace = self._repo_repo.get_workspace(
                self._config.workspace_root,
                self._config.session_prefix,
                self._config.main_branch,
            )
            envs = self._worktree_repo.get_environments(workspace, project_repos)
        except RepoError as exc:
            return [
                ProbeResult(
                    source=CORE_SOURCE,
                    name="envs",
                    status=ProbeStatus.fail,
                    message=f"failed to enumerate envs: {exc}",
                )
            ]
        return [self._probe_env(env, project_repos) for env in envs]

    def _probe_env(self, env: FeatureEnvironment, project_repos: list[ProjectRepository]) -> ProbeResult:
        # Every worktree in an env — pinned or not — lives on a local branch
        # named after the env. Pinned repos differ only in their upstream
        # tracking ref (origin/<main>) and aren't audited here.
        label = f"env: {env.name}"
        problems: list[str] = []
        for repo in project_repos:
            worktree_path = env.path / repo.name
            if not self._fs.is_dir(worktree_path):
                problems.append(f"{repo.name} missing")
                continue
            actual_branch = self._read_branch(worktree_path)
            if actual_branch is None:
                problems.append(f"{repo.name} branch unknown")
            elif actual_branch != env.name:
                problems.append(f"{repo.name} on `{actual_branch}` (expected `{env.name}`)")
        if problems:
            return ProbeResult(
                source=CORE_SOURCE,
                name=label,
                status=ProbeStatus.fail,
                message="; ".join(problems),
                remediation=f"Run `winter ws init {env.name}` to reconcile worktrees.",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name=label,
            status=ProbeStatus.pass_,
            message=f"{len(project_repos)} worktrees consistent",
        )

    # ── .claude/{agents,skills} symlink health ───────────────────────────

    def _probe_claude_symlinks(self) -> ProbeResult | None:
        """Detect broken `.claude/{agents,skills}/*` symlinks left by extension renames.

        `ExtensionSymlinkService._prune_stale_symlinks` heals these on the next
        `winter ws init`, but until then a stale link silently shadows a renamed
        agent or skill — the failure surfaces only when something tries to spawn
        the missing target. Returns None when neither directory exists so the
        probe stays quiet on workspaces that never adopted extensions.
        """
        claude_root = self._config.workspace_root / ".claude"
        candidates = (claude_root / "agents", claude_root / "skills")
        any_dir_present = False
        orphans: list[str] = []
        for directory in candidates:
            if not self._fs.is_dir(directory):
                continue
            any_dir_present = True
            for entry in self._fs.iterdir(directory):
                if not self._fs.is_symlink(entry):
                    continue
                # exists() follows symlinks — a broken link reports False.
                if self._fs.exists(entry):
                    continue
                orphans.append(str(entry.relative_to(self._config.workspace_root)))
        if not any_dir_present:
            return None
        if orphans:
            return ProbeResult(
                source=CORE_SOURCE,
                name=".claude symlinks",
                status=ProbeStatus.fail,
                message=f"orphaned symlink(s): {', '.join(sorted(orphans))}",
                remediation="Run `winter ws init` to prune stale extension symlinks.",
            )
        return ProbeResult(
            source=CORE_SOURCE,
            name=".claude symlinks",
            status=ProbeStatus.pass_,
            message="no orphaned symlinks",
        )

    def _read_branch(self, worktree_path: Path) -> str | None:
        try:
            result = self._subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch or None
