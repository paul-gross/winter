from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

from winter_cli.config.models import WorkspaceConfig
from winter_cli.core.filesystem import IFilesystemWriter
from winter_cli.modules.workspace.init_reporter import IInitReporter

# Filename written at the workspace root and at each feature-env root.
AGENTS_MD_FILENAME = "AGENTS.md"

# The workspace CLAUDE.md whose @imports define the root of the graph.
CLAUDE_MD_FILENAME = "CLAUDE.md"

# Source label for reporter calls.
_BLOCK_NAME = "winter-agents"

# @import pattern: matches both line-leading and inline-in-prose @path references.
# Kept in sync by hand with winter-lint extractability.py — update both when changing.
_IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s`]+)")

# Trailing punctuation to strip from a captured @import path.
# Kept in sync by hand with winter-lint extractability.py — update both when changing.
_IMPORT_TRIM = ".,;:!?)]}>\"'"


def _iter_line_imports(
    text: str,
) -> "Iterator[tuple[str, bool, list[tuple[re.Match[str], str]]]]":
    """Walk every line of `text`, yielding fence-aware import information.

    Yields ``(original_line, passthrough, matches)`` for each line:

    * ``original_line`` — the line as it appears in ``text`` (with line ending).
    * ``passthrough`` — ``True`` when the line is a fence boundary or falls
      inside a fenced code block; callers should emit it verbatim and ignore
      ``matches``.
    * ``matches`` — list of ``(match_object, raw_path)`` tuples for every
      ``@import`` reference found on this line (empty for passthrough lines and
      lines with no valid import tokens).

    This is the **single implementation** of the fence-tracking + regex-walk +
    path-guard logic.  Both :func:`_extract_import_paths` and
    :meth:`_ImportGraphExpander._expand_text` delegate to it so the two callers
    cannot drift.
    """
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield line, True, []
            continue
        if in_fence:
            yield line, True, []
            continue
        raw_line = line.rstrip("\n")
        matches: list[tuple[re.Match[str], str]] = []
        for m in _IMPORT_RE.finditer(raw_line):
            raw = m.group(1).rstrip(_IMPORT_TRIM)
            if "/" not in raw and "." not in raw:
                continue
            matches.append((m, raw))
        yield line, False, matches


def _extract_import_paths(text: str) -> list[str]:
    """Return all @import paths found in `text`, in order of appearance.

    Recognises both line-leading and inline-in-prose @path references.  Strips
    trailing prose punctuation.  Skips @word tokens that have neither a slash
    nor a dot (e.g. ``@param``, ``@deprecated``) because those are never file
    paths.  Skips tokens inside fenced code blocks (``` / ~~~) — consistent with
    how extractability.py treats code fences.

    Delegates to :func:`_iter_line_imports` so there is exactly one extraction
    walk in this module.
    """
    paths: list[str] = []
    for _line, passthrough, matches in _iter_line_imports(text):
        if passthrough:
            continue
        for _m, raw in matches:
            paths.append(raw)
    return paths


def _compute_hash(content: str) -> str:
    """Return a SHA-256 hex digest of the UTF-8 encoding of `content`."""
    return hashlib.sha256(content.encode()).hexdigest()


class _ImportGraphExpander:
    """Recursively expands @import references in a CLAUDE.md into a flat string.

    All behavior is in service-class methods so that the filesystem collaborator
    lives on an instance rather than as a free-function parameter.
    """

    def __init__(self, base_dir: Path, fs: IFilesystemWriter) -> None:
        self._base_dir = base_dir
        self._fs = fs
        self._visited: set[Path] = set()

    def expand(self, file: Path) -> str:
        """Expand `file`, inlining the content of every @import recursively."""
        return self._expand_file(file, importing_dir=self._base_dir)

    def _expand_file(self, file: Path, importing_dir: Path) -> str:
        resolved = file.resolve()
        if resolved in self._visited:
            return f"<!-- AGENTS.md: skipped circular import {file} -->\n"
        self._visited.add(resolved)

        try:
            text = self._fs.read_text(file)
        except (OSError, FileNotFoundError):
            return f"<!-- AGENTS.md: missing import {file} -->\n"

        return self._expand_text(text, importing_dir=file.parent)

    def _expand_text(self, text: str, importing_dir: Path) -> str:
        """Expand all @import lines in `text`, returning the flattened content.

        Each nested @import is resolved relative to `importing_dir` (the directory
        of the file that contains it), matching the resolution semantics of
        extractability.py.

        Delegates fence-tracking and match extraction to :func:`_iter_line_imports`
        so there is exactly one extraction walk in this module.
        """
        output_parts: list[str] = []

        for line, passthrough, imports in _iter_line_imports(text):
            if passthrough or not imports:
                output_parts.append(line)
                continue

            # Build a version of the line with all @path tokens removed so the
            # output contains no unresolved references.  Walk right to left so
            # that index positions remain valid after each removal.
            raw_line = line.rstrip("\n")
            modified = raw_line
            for m, raw in reversed(imports):
                token_end = m.start() + 1 + len(raw)
                modified = modified[: m.start()] + modified[token_end:]

            eol = "\n" if line.endswith("\n") else ""
            cleaned = modified.rstrip() + eol
            if cleaned.strip():
                output_parts.append(cleaned)

            # Inject the expansion of each referenced file in original order.
            # Resolve each @import relative to the importing file's directory so
            # that nested imports in subdirectories resolve to their siblings —
            # matching extractability.py's ``file.parent / raw`` resolution.
            for _m, raw_path in imports:
                import_file = (importing_dir / raw_path).resolve()
                expansion = self._expand_file(import_file, importing_dir=import_file.parent)
                output_parts.append(f"\n<!-- imported from {raw_path} -->\n")
                output_parts.append(expansion)
                output_parts.append(f"<!-- end {raw_path} -->\n")

        return "".join(output_parts)


class AgentsMdService:
    """Generates a flat ``AGENTS.md`` at the workspace root and per feature env.

    The file is the recursively resolved, inlined expansion of the auto-imported
    Claude context starting from ``CLAUDE.md`` at the given root.  It is
    deterministic and idempotent: re-running on unchanged sources produces a
    byte-identical file.

    Generation is a no-op when there is no ``CLAUDE.md`` at the root.
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        fs: IFilesystemWriter,
    ) -> None:
        self._config = config
        self._fs = fs

    def generate(
        self,
        root: Path,
        reporter: IInitReporter,
    ) -> bool:
        """Generate (or update) ``AGENTS.md`` under `root`.

        Returns True on success (including no-op when already up-to-date),
        False on I/O error.
        """
        claude_md = root / CLAUDE_MD_FILENAME
        if not self._fs.is_file(claude_md):
            return True

        agents_path = root / AGENTS_MD_FILENAME
        try:
            content = self._build_content(claude_md)
            existing = self._fs.read_text(agents_path) if self._fs.exists(agents_path) else ""
            if content == existing:
                return True
            self._fs.write_text(agents_path, content)
        except OSError as exc:
            reporter.repo_error(_BLOCK_NAME, f"{AGENTS_MD_FILENAME} — {exc}")
            return False

        reporter.repo_action(
            _BLOCK_NAME,
            str(agents_path),
            "agents_md_generated",
            str(claude_md),
        )
        return True

    def content_hash(self, root: Path) -> str | None:
        """Return the SHA-256 hash of the resolved AGENTS.md content for `root`.

        Returns None when there is no ``CLAUDE.md`` at `root` (nothing to generate).
        """
        claude_md = root / CLAUDE_MD_FILENAME
        if not self._fs.is_file(claude_md):
            return None
        content = self._build_content(claude_md)
        return _compute_hash(content)

    def _build_content(self, claude_md: Path) -> str:
        """Resolve and inline the full @import graph starting from `claude_md`."""
        base_dir = claude_md.parent
        expander = _ImportGraphExpander(base_dir=base_dir, fs=self._fs)
        resolved = expander.expand(claude_md)
        header = (
            "# AGENTS.md\n\n"
            "This file is auto-generated by `winter ws init`. "
            "Do not edit by hand — re-run init to regenerate.\n\n"
            "<!-- Generated from CLAUDE.md with @import graph resolved -->\n\n"
        )
        return header + resolved
