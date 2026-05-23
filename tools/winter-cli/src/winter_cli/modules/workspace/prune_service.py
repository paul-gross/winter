from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.extension_exclude_service import ExtensionExcludeService
from winter_cli.modules.workspace.git_repository import IGitRepository
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.repository_factory import RepositoryFactory

logger = logging.getLogger(__name__)

_BLOCK_BEGIN_RE = re.compile(r"^# >>> ([^/]+?) \(managed by winter\)$")
_BLOCK_PATH_RE = re.compile(r"^/(.+?)/?$")


@dataclasses.dataclass
class PruneOrphan:
    kind: str
    """One of: 'project_clone', 'standalone_clone', 'broken_symlink'."""

    path: Path
    """Absolute path of the orphan on disk."""

    safe_to_remove: bool
    """False when removing would discard work or break worktrees."""

    notes: str
    """Empty when safe; otherwise the reason it's blocked."""


class PruneService:
    """Detects and removes disk state for repos no longer in the workspace config."""

    def __init__(
        self,
        config: WorkspaceConfig,
        repo_factory: RepositoryFactory,
        extension_exclude_svc: ExtensionExcludeService,
        fs: IFilesystemWriter,
        git_repo: IGitRepository,
    ) -> None:
        self._config = config
        self._repo_factory = repo_factory
        self._extension_exclude_svc = extension_exclude_svc
        self._fs = fs
        self._git_repo = git_repo

    def find_orphans(self) -> list[PruneOrphan]:
        logger.info("find_orphans start")
        orphans: list[PruneOrphan] = []
        orphans.extend(self._find_orphan_project_clones())
        orphans.extend(self._find_orphan_standalone_clones())
        orphans.extend(self._find_broken_symlinks())
        logger.info("find_orphans done: %d orphan(s)", len(orphans))
        return orphans

    def remove_orphan(self, orphan: PruneOrphan) -> None:
        if not orphan.safe_to_remove:
            logger.warning("remove_orphan: refusing unsafe orphan %s (%s)", orphan.path, orphan.notes)
            raise RuntimeError(f"refusing to remove unsafe orphan: {orphan.path} ({orphan.notes})")
        logger.info("remove_orphan: %s (%s)", orphan.path, orphan.kind)
        if self._fs.is_symlink(orphan.path):
            self._fs.unlink(orphan.path)
        elif self._fs.is_dir(orphan.path):
            self._fs.rmtree(orphan.path)
        elif self._fs.exists(orphan.path):
            self._fs.unlink(orphan.path)

    def reaggregate_excludes(self, reporter: IInitReporter) -> bool:
        return self._extension_exclude_svc.finalize_excludes(self._repo_factory.get_standalone_repos(), reporter)

    # ── detection ────────────────────────────────────────────────────────

    def _find_orphan_project_clones(self) -> list[PruneOrphan]:
        projects_dir = self._config.workspace_root / "projects"
        if not self._fs.is_dir(projects_dir):
            return []
        declared = {repo.name for repo in self._repo_factory.get_project_repos()}
        orphans: list[PruneOrphan] = []
        for entry in sorted(self._fs.iterdir(projects_dir)):
            if not self._fs.is_dir(entry) or entry.name in declared:
                continue
            safe, notes = self._project_clone_safety(entry)
            orphans.append(
                PruneOrphan(
                    kind="project_clone",
                    path=entry,
                    safe_to_remove=safe,
                    notes=notes,
                )
            )
        return orphans

    def _find_orphan_standalone_clones(self) -> list[PruneOrphan]:
        exclude_path = self._config.workspace_root / ".git" / "info" / "exclude"
        if not self._fs.exists(exclude_path):
            return []
        try:
            content = self._fs.read_text(exclude_path)
        except OSError:
            return []

        eligible = {repo.name for repo in self._repo_factory.get_standalone_repos()}
        orphans: list[PruneOrphan] = []
        seen_paths: set[Path] = set()

        for block_name, block_lines in self._iter_managed_blocks(content):
            if block_name in eligible:
                continue
            for line in block_lines:
                m = _BLOCK_PATH_RE.match(line.strip())
                if not m:
                    continue
                rel = m.group(1)
                if rel.startswith(".claude/"):
                    continue
                if rel == "projects":
                    continue
                path = (self._config.workspace_root / rel).resolve()
                if path in seen_paths or not self._fs.exists(path):
                    continue
                seen_paths.add(path)
                safe, notes = self._project_clone_safety(path) if self._fs.exists(path / ".git") else (True, "")
                orphans.append(
                    PruneOrphan(
                        kind="standalone_clone",
                        path=path,
                        safe_to_remove=safe,
                        notes=notes,
                    )
                )
        return orphans

    def _find_broken_symlinks(self) -> list[PruneOrphan]:
        roots = [
            self._config.workspace_root / ".claude" / "skills",
            self._config.workspace_root / ".claude" / "agents",
        ]
        orphans: list[PruneOrphan] = []
        for root in roots:
            if not self._fs.is_dir(root):
                continue
            for entry in sorted(self._fs.iterdir(root)):
                if self._fs.is_symlink(entry) and not self._fs.exists(entry):
                    orphans.append(
                        PruneOrphan(
                            kind="broken_symlink",
                            path=entry,
                            safe_to_remove=True,
                            notes="",
                        )
                    )
        return orphans

    @staticmethod
    def _iter_managed_blocks(content: str):
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            m = _BLOCK_BEGIN_RE.match(lines[i])
            if not m:
                i += 1
                continue
            name = m.group(1)
            end_marker = f"# <<< {name}"
            j = i + 1
            block_lines: list[str] = []
            while j < len(lines) and lines[j] != end_marker:
                block_lines.append(lines[j])
                j += 1
            yield name, block_lines
            i = j + 1

    def _project_clone_safety(self, path: Path) -> tuple[bool, str]:
        if not self._fs.exists(path / ".git"):
            return False, "not a git clone (delete by hand if intentional)"
        worktrees_dir = path / ".git" / "worktrees"
        if self._fs.is_dir(worktrees_dir) and self._fs.iterdir(worktrees_dir):
            return False, "has linked worktrees"
        if not self._git_repo.is_worktree_clean(path):
            return False, "uncommitted or untracked changes"
        return True, ""
