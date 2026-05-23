from __future__ import annotations

import logging
import os
from pathlib import Path

from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.extension_manifest import (
    EXT_MANIFEST,
    ExtensionManifestLoader,
)
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

logger = logging.getLogger(__name__)


class ExtensionSymlinkService:
    """Installs `.claude/skills/<prefix>-*` and `.claude/agents/<prefix>-*` symlinks for an extension repo.

    For each standalone repo, decides whether it should contribute skills/agents
    (per `adopt_extensions` mode and the presence of `winter-ext.toml`),
    validates SKILL.md frontmatter conforms to the prefix-by-directory
    convention, and creates per-entry symlinks under
    `.claude/skills/<prefix>-<dir>` and `.claude/agents/<prefix>-<dir>`.

    Error-handling shape: `process` is the wrap site. Leaves raise
    `RepoError` / `OSError`; one try/except at the boundary routes the
    failure through the reporter.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemWriter,
        manifest_loader: ExtensionManifestLoader,
    ) -> None:
        self._config = config
        self._fs = fs
        self._manifest_loader = manifest_loader

    def process(
        self,
        repo: StandaloneRepository,
        reporter: IInitReporter,
    ) -> bool:
        logger.info("process symlinks: repo=%s", repo.name)
        mode = self._config.adopt_extensions
        if mode == AdoptExtensions.none:
            return True

        manifest_path = repo.path / EXT_MANIFEST
        manifest_present = self._fs.is_file(manifest_path)

        if mode == AdoptExtensions.winter and not manifest_present:
            logger.info("process symlinks: %s skipped (winter mode, no manifest)", repo.name)
            return True

        try:
            manifest = self._manifest_loader.load(repo, manifest_path if manifest_present else None)
            skills_root = self._resolve_existing_dir(repo.path, manifest.skills_dirs)
            agents_root = self._resolve_existing_dir(repo.path, manifest.agents_dirs)

            self._validate_frontmatter(repo, skills_root, reporter, strict=mode == AdoptExtensions.winter)

            # Skills are always directories containing SKILL.md.
            skills_target = self._config.workspace_root / ".claude" / "skills"
            skill_links = self._symlink_entries(
                source_root=skills_root,
                target_root=skills_target,
                prefix=manifest.prefix,
                kind="skill",
                include_dirs=True,
                include_files=False,
                require_marker_file="SKILL.md",
            )
            self._prune_stale_symlinks(skills_target, manifest.prefix, set(skill_links), kind="skill")

            # Agents are flat .md files (one per agent). Directories are
            # reserved for the nested-agent convention and must carry an
            # AGENT.md marker; bare doc directories (e.g. `agents/docs/`) and
            # `README.md` files at the agents root are skipped.
            agents_target = self._config.workspace_root / ".claude" / "agents"
            agent_links = self._symlink_entries(
                source_root=agents_root,
                target_root=agents_target,
                prefix=manifest.prefix,
                kind="agent",
                include_dirs=True,
                include_files=True,
                file_suffix=".md",
                exclude_filenames=("README.md",),
                require_marker_file="AGENT.md",
            )
            self._prune_stale_symlinks(agents_target, manifest.prefix, set(agent_links), kind="agent")
        except (RepoError, OSError) as exc:
            logger.warning("process symlinks: failed for %s — %s", repo.name, exc)
            reporter.repo_error(repo.name, str(exc))
            return False

        if skill_links or agent_links:
            detail = f"prefix={manifest.prefix} skills={len(skill_links)} agents={len(agent_links)}"
            reporter.repo_action(repo.name, str(repo.path), "extension_installed", detail)

        return True

    # ── Frontmatter validation ────────────────────────────────────────────

    def _validate_frontmatter(
        self,
        repo: StandaloneRepository,
        skills_root: Path | None,
        reporter: IInitReporter,
        strict: bool,
    ) -> None:
        """Ensure SKILL.md files don't override the symlinked directory name.

        Claude Code lets the `name` frontmatter field override the directory name
        when discovering skills — that defeats the prefix-by-symlink design. In
        strict (`winter`) mode, raise so the wrap site fails the install. In
        `all` mode, the user opts into a less-curated experience, so we only warn.
        """
        if skills_root is None or not self._fs.is_dir(skills_root):
            return

        offenders: list[str] = []
        for entry in sorted(self._fs.iterdir(skills_root)):
            if not self._fs.is_dir(entry):
                continue
            skill_md = entry / "SKILL.md"
            if not self._fs.is_file(skill_md):
                continue
            name_field = self._extract_frontmatter_name(skill_md)
            if name_field is None:
                continue
            offenders.append(f"{entry.name}/SKILL.md sets `name: {name_field}`")

        if not offenders:
            return

        msg = (
            f"extension {repo.name} has SKILL.md files with frontmatter `name` set, "
            f"which would override the prefixed directory name and break namespacing. "
            f"Remove the `name` field so the directory name (set by winter) is authoritative. "
            f"Offenders: {'; '.join(offenders)}"
        )
        if strict:
            raise RepoError(msg)
        # adopt_extensions = "all": warn via repo_action so the user sees it but install proceeds.
        reporter.repo_action(repo.name, str(repo.path), "extension_warning", msg)

    def _extract_frontmatter_name(self, skill_md: Path) -> str | None:
        """Return the `name` field from YAML frontmatter, or None if not set.

        Looks only at the top-level frontmatter delimited by `---`. Returns None
        if there's no frontmatter, no `name` key, or any read error.
        """
        try:
            text = self._fs.read_text(skill_md)
        except OSError:
            return None
        if not text.startswith("---"):
            return None
        # Find closing delimiter.
        lines = text.split("\n")
        if len(lines) < 2:
            return None
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            return None
        for line in lines[1:end_idx]:
            stripped = line.strip()
            if stripped.startswith("name:"):
                value = stripped.split(":", 1)[1].strip()
                # Strip optional surrounding quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if value:
                    return value
        return None

    # ── Symlinks ──────────────────────────────────────────────────────────

    def _resolve_existing_dir(self, base: Path, candidates: tuple[str, ...]) -> Path | None:
        """Return the first candidate path under `base` that exists as a directory."""
        for candidate in candidates:
            path = base / candidate
            if self._fs.is_dir(path):
                return path
        return None

    def _symlink_entries(
        self,
        source_root: Path | None,
        target_root: Path,
        prefix: str,
        kind: str,
        include_dirs: bool,
        include_files: bool,
        file_suffix: str = "",
        exclude_filenames: tuple[str, ...] = (),
        require_marker_file: str | None = None,
    ) -> list[str]:
        """Create one symlink per matching entry in `source_root`.

        For directory entries the symlink keeps the directory name (`<prefix>-<dirname>`).
        For file entries with a matching suffix the symlink keeps the full filename
        (`<prefix>-<filename>`), so a `.md` extension is preserved.

        `exclude_filenames` skips matching file entries by exact basename — used to
        keep `README.md` out of the installed agent set. `require_marker_file`
        restricts directory entries to those containing that marker file (e.g.
        `SKILL.md`, `AGENT.md`), so doc-only subdirectories don't masquerade as
        skills or nested agents.

        Returns the list of created/existing symlink names. Empty when `source_root`
        is None or doesn't exist. Raises `RepoError` on conflict or I/O failure.
        """
        if source_root is None or not self._fs.is_dir(source_root):
            return []

        self._fs.mkdir(target_root, parents=True, exist_ok=True)

        linked: list[str] = []
        for entry in sorted(self._fs.iterdir(source_root)):
            if self._fs.is_dir(entry):
                if not include_dirs:
                    continue
                if require_marker_file is not None and not self._fs.is_file(entry / require_marker_file):
                    continue
                link_name = f"{prefix}-{entry.name}"
            elif self._fs.is_file(entry):
                if not include_files:
                    continue
                if file_suffix and not entry.name.endswith(file_suffix):
                    continue
                if entry.name in exclude_filenames:
                    continue
                link_name = f"{prefix}-{entry.name}"
            else:
                continue

            link_path = target_root / link_name
            relative_target = self._relative_symlink_target(target_root, entry)

            if self._fs.is_symlink(link_path):
                # Update if pointing at the wrong place.
                try:
                    current = self._fs.readlink(link_path)
                except OSError:
                    current = None
                if current != relative_target:
                    try:
                        self._fs.unlink(link_path)
                        self._fs.symlink_to(link_path, relative_target)
                    except OSError as exc:
                        raise RepoError(f"refresh {kind} symlink {link_name}: {exc}") from exc
                linked.append(link_name)
                continue

            if self._fs.exists(link_path):
                raise RepoError(
                    f"cannot create {kind} symlink {link_name}: path exists and is not a symlink",
                )

            try:
                self._fs.symlink_to(link_path, relative_target)
            except OSError as exc:
                raise RepoError(f"create {kind} symlink {link_name}: {exc}") from exc
            linked.append(link_name)

        return linked

    @staticmethod
    def _relative_symlink_target(link_dir: Path, target: Path) -> Path:
        """Compute the symlink target as a path relative to the link's parent directory.

        Relative targets keep the workspace portable — moving the workspace doesn't
        invalidate the links.
        """
        return Path(os.path.relpath(target, link_dir))

    def _prune_stale_symlinks(
        self,
        target_root: Path,
        prefix: str,
        live_names: set[str],
        kind: str,
    ) -> None:
        """Remove any `<prefix>-*` symlinks in `target_root` that weren't created this pass.

        Catches two cases:
          - the source entry was deleted upstream (broken symlink like the
            historical `wf-blizzard` after the source `agents/blizzard/`
            directory went away);
          - the source entry still exists but is now filtered out by the
            install pass (README.md, AGENT.md-less directories).

        Only symlinks whose name starts with `f"{prefix}-"` are considered —
        each extension owns its prefix, so this won't touch other extensions'
        links or user-placed files. Raises `RepoError` on I/O failure.
        """
        if not self._fs.is_dir(target_root):
            return

        prefix_with_dash = f"{prefix}-"
        for entry in sorted(self._fs.iterdir(target_root)):
            if not self._fs.is_symlink(entry):
                continue
            if not entry.name.startswith(prefix_with_dash):
                continue
            if entry.name in live_names:
                continue
            try:
                self._fs.unlink(entry)
            except OSError as exc:
                raise RepoError(f"prune stale {kind} symlink {entry.name}: {exc}") from exc
