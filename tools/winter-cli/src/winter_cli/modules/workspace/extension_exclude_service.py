from __future__ import annotations

import logging
import re

from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.extension_manifest import (
    CLAUDEMD_BLOCK_NAME,
    EXT_MANIFEST,
    ExtensionManifestLoader,
)
from winter_cli.modules.workspace.init_reporter import IInitReporter
from winter_cli.modules.workspace.internal.managed_block import (
    GITIGNORE_BEGIN,
    GITIGNORE_END,
    replace_or_append_block,
    strip_block,
)
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

logger = logging.getLogger(__name__)


class ExtensionExcludeService:
    """Aggregate-updates the workspace `.git/info/exclude` with one block per extension repo.

    Each block is bracketed with `# >>> <name> (managed by winter)` markers
    and lists the extension repo path plus the symlink globs under
    `.claude/skills/` and `.claude/agents/`. Orphan blocks for extensions
    no longer present are stripped automatically.
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

    def finalize_excludes(
        self,
        repos: list[StandaloneRepository],
        reporter: IInitReporter,
    ) -> bool:
        """Aggregate-update the workspace `.git/info/exclude` with one block per extension.

        Called once after all standalones are reconciled. Each block is bracketed
        with `# >>> <name> (managed by winter)` markers and lists the extension
        repo path plus the symlink globs under `.claude/skills/` and `.claude/agents/`.
        Orphan blocks for extensions no longer present are stripped automatically;
        if no extensions are eligible, every winter-managed block is removed.
        """
        logger.info("finalize_excludes start: %d repo(s)", len(repos))
        if self._config.adopt_extensions == AdoptExtensions.none:
            logger.info("finalize_excludes: adopt_extensions=none, skipping")
            return True

        exclude_path = self._config.workspace_root / ".git" / "info" / "exclude"

        eligible: list[tuple[str, list[str]]] = []
        for repo in repos:
            resolved = self._resolve_for_excludes(repo, reporter)
            if resolved is None:
                continue
            relative, prefix = resolved
            begin = GITIGNORE_BEGIN.format(name=repo.name)
            end = GITIGNORE_END.format(name=repo.name)
            lines = [begin, f"/{relative}/"]
            if prefix is not None:
                lines.extend(
                    [
                        f".claude/skills/{prefix}-*",
                        f".claude/agents/{prefix}-*",
                    ]
                )
            lines.append(end)
            eligible.append((repo.name, lines))

        eligible_names = {name for name, _ in eligible}

        try:
            existing = self._fs.read_text(exclude_path) if self._fs.exists(exclude_path) else ""
            new_content = self._strip_orphan_managed_blocks(existing, eligible_names)
            for block_name, desired_lines in eligible:
                begin = GITIGNORE_BEGIN.format(name=block_name)
                end = GITIGNORE_END.format(name=block_name)
                new_content = replace_or_append_block(new_content, begin, end, desired_lines)
            if new_content == existing:
                return True
            self._fs.mkdir(exclude_path.parent, parents=True, exist_ok=True)
            self._fs.write_text(exclude_path, new_content)
        except OSError as exc:
            logger.warning("finalize_excludes: write failed at %s — %s", exclude_path, exc)
            reporter.repo_error(CLAUDEMD_BLOCK_NAME, f".git/info/exclude — {exc}")
            return False

        detail = ", ".join(sorted(eligible_names)) if eligible_names else "cleared"
        reporter.repo_action(
            CLAUDEMD_BLOCK_NAME,
            str(exclude_path),
            "workspace_excludes_updated",
            detail,
        )
        return True

    def _resolve_for_excludes(
        self,
        repo: StandaloneRepository,
        reporter: IInitReporter,
    ) -> tuple[str, str | None] | None:
        """Resolve (relative_path, prefix) for an extension's exclude block, or None if not eligible.

        Every standalone repo cloned at a path under the workspace root gets its
        directory added to `.git/info/exclude` so it doesn't appear as untracked
        in the workspace repo — this applies regardless of manifest presence or
        adopt_extensions mode.

        `prefix` is only returned when the repo actually contributes symlinks
        under `.claude/skills/` or `.claude/agents/` (i.e. is being processed as
        an extension). When `prefix` is None, no symlink-glob lines are added
        to the exclude block — a broken-TOML repo still gets its path exclude
        so the workspace doesn't show it as untracked, but loses its symlink
        globs (the loader has already reported the TOML error to the same
        reporter).
        """
        if not self._fs.exists(repo.path):
            return None
        try:
            relative = repo.path.relative_to(self._config.workspace_root).as_posix()
        except ValueError:
            return None
        mode = self._config.adopt_extensions
        manifest_path = repo.path / EXT_MANIFEST
        manifest_present = self._fs.is_file(manifest_path)
        extension_eligible = mode != AdoptExtensions.none and (manifest_present or mode == AdoptExtensions.all)
        if not extension_eligible:
            return relative, None
        try:
            manifest = self._manifest_loader.load(repo, manifest_path if manifest_present else None)
        except (RepoError, OSError) as exc:
            # Broken manifest — report it but still keep the repo's directory
            # exclude so the workspace doesn't show it as untracked. Drop the
            # symlink globs because we couldn't resolve a reliable prefix.
            reporter.repo_error(repo.name, str(exc))
            return relative, None
        return relative, manifest.prefix

    @staticmethod
    def _strip_orphan_managed_blocks(content: str, eligible_names: set[str]) -> str:
        """Remove any `# >>> X (managed by winter)` block whose X is not in `eligible_names`.

        The regex deliberately rejects names containing `/` so that namespaced
        blocks owned by other subsystems (e.g. `winter-dir/projects` written by
        InitService) are not treated as orphans of the extension flow.
        """
        pattern = re.compile(r"^# >>> ([^/]+?) \(managed by winter\)$")
        orphan_names: set[str] = set()
        for line in content.split("\n") if content else []:
            m = pattern.match(line)
            if m:
                name = m.group(1)
                if name not in eligible_names:
                    orphan_names.add(name)
        result = content
        for name in orphan_names:
            begin = GITIGNORE_BEGIN.format(name=name)
            end = GITIGNORE_END.format(name=name)
            result = strip_block(result, begin, end)
        return result
