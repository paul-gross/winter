"""Tests for AgentsMdService: @import graph resolution and AGENTS.md generation.

Covers:
  1. Full recursive resolution of a nested @import graph.
  2. Idempotency: re-running on unchanged sources produces byte-identical output.
  3. No-op when CLAUDE.md does not exist at root.
  4. Files with circular @imports don't loop forever; they emit a comment.
  5. Missing imported files emit a comment rather than raising.
  6. In-prose @imports (not line-leading) are also resolved.
  7. @imports inside fenced code blocks are NOT expanded.
  8. No unresolved @path references remain in the output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem, FakeInitReporter
from winter_cli.config.models import AdoptExtensions, WorkspaceConfig
from winter_cli.modules.workspace.agents_md_service import (
    AGENTS_MD_FILENAME,
    AgentsMdService,
    _ImportGraphExpander,
    _extract_import_paths,
)

WORKSPACE_ROOT = Path("/ws")


@pytest.fixture
def workspace_config() -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        session_prefix="t",
        main_branch="main",
        adopt_extensions=AdoptExtensions.winter,
    )


# ---------------------------------------------------------------------------
# Unit tests for _extract_import_paths
# ---------------------------------------------------------------------------


def test_extract_import_paths_line_leading() -> None:
    text = "@ai/project/index.md\n"
    assert _extract_import_paths(text) == ["ai/project/index.md"]


def test_extract_import_paths_inline_in_prose() -> None:
    text = "IMPORTANT: This workspace has fundamental pieces declared in @ai/project/index.md that are pertinent.\n"
    assert _extract_import_paths(text) == ["ai/project/index.md"]


def test_extract_import_paths_skips_code_fence() -> None:
    text = "```\n@ai/project/index.md\n```\n"
    assert _extract_import_paths(text) == []


def test_extract_import_paths_skips_at_param_tokens() -> None:
    text = "See @param for details and @deprecated function.\n"
    assert _extract_import_paths(text) == []


def test_extract_import_paths_multiple_on_one_line() -> None:
    text = "See @ai/a.md and @ai/b.md for details.\n"
    result = _extract_import_paths(text)
    assert "ai/a.md" in result
    assert "ai/b.md" in result


def test_extract_import_paths_strips_trailing_punctuation() -> None:
    text = "See @ai/a.md.\n"
    assert _extract_import_paths(text) == ["ai/a.md"]


# ---------------------------------------------------------------------------
# Unit tests for _ImportGraphExpander
# ---------------------------------------------------------------------------


def _expand(fs: FakeFilesystem, files: dict) -> str:
    """Helper: build expander from a files dict rooted at WORKSPACE_ROOT/CLAUDE.md."""
    expander = _ImportGraphExpander(base_dir=WORKSPACE_ROOT, fs=fs)
    return expander.expand(WORKSPACE_ROOT / "CLAUDE.md")


def test_resolve_import_graph_simple() -> None:
    """A CLAUDE.md importing one file inlines that file's content."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "# AI Index\nSome content.\n",
        }
    )
    result = _expand(fs, {})
    assert "# AI Index" in result
    assert "Some content." in result


def test_resolve_import_graph_nested() -> None:
    """A transitively imported file's content is also inlined (recursive resolution)."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@level1.md\n",
            WORKSPACE_ROOT / "level1.md": "Level1\n@level2.md\n",
            WORKSPACE_ROOT / "level2.md": "Level2 content\n",
        }
    )
    result = _expand(fs, {})
    assert "Level1" in result
    assert "Level2 content" in result


def test_resolve_import_graph_circular_import_no_infinite_loop() -> None:
    """Circular imports do not cause infinite recursion — they emit a comment."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@a.md\n",
            WORKSPACE_ROOT / "a.md": "A\n@b.md\n",
            WORKSPACE_ROOT / "b.md": "B\n@a.md\n",
        }
    )
    result = _expand(fs, {})
    assert "A" in result
    assert "B" in result
    assert "circular" in result.lower() or "skipped" in result.lower()


def test_resolve_import_graph_missing_import_emits_comment() -> None:
    """Missing imported files result in a comment rather than raising."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@missing.md\n",
        }
    )
    result = _expand(fs, {})
    assert "missing" in result.lower() or "import" in result.lower()


def test_resolve_import_graph_code_fence_not_expanded() -> None:
    """@imports inside fenced code blocks are NOT inlined."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "```\n@ai/index.md\n```\n",
            WORKSPACE_ROOT / "ai" / "index.md": "Should NOT appear\n",
        }
    )
    result = _expand(fs, {})
    assert "Should NOT appear" not in result


def test_nested_import_resolves_relative_to_importing_file() -> None:
    """A nested @import is resolved relative to the file that contains it.

    CLAUDE.md imports ai/index.md. ai/index.md in turn imports a sibling
    (ai/detail.md) using a bare relative path. The expander must resolve
    ``detail.md`` against ``ai/`` (the directory of ai/index.md), not against
    the workspace root. No "missing import" comment must appear in the output.
    """
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "Index\n@detail.md\n",
            WORKSPACE_ROOT / "ai" / "detail.md": "Detail content\n",
        }
    )
    result = _expand(fs, {})
    assert "Detail content" in result
    assert "missing import" not in result.lower()


# ---------------------------------------------------------------------------
# AgentsMdService integration tests
# ---------------------------------------------------------------------------


def test_generate_creates_agents_md(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """generate() writes AGENTS.md containing the inlined content."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/project/index.md\n",
            WORKSPACE_ROOT / "ai" / "project" / "index.md": "# Project Index\n",
        }
    )
    svc = AgentsMdService(config=workspace_config, fs=fs)

    ok = svc.generate(WORKSPACE_ROOT, init_reporter)

    assert ok is True
    agents_path = WORKSPACE_ROOT / AGENTS_MD_FILENAME
    assert agents_path in fs.files
    content = fs.files[agents_path]
    assert "# Project Index" in content


def test_generate_no_op_when_no_claude_md(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """generate() is a no-op (returns True, writes nothing) when CLAUDE.md is absent."""
    fs = FakeFilesystem()
    svc = AgentsMdService(config=workspace_config, fs=fs)

    ok = svc.generate(WORKSPACE_ROOT, init_reporter)

    assert ok is True
    assert (WORKSPACE_ROOT / AGENTS_MD_FILENAME) not in fs.files


def test_generate_idempotent(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """Calling generate() twice on unchanged sources produces byte-identical output."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "# AI Index\n",
        }
    )
    svc = AgentsMdService(config=workspace_config, fs=fs)

    ok1 = svc.generate(WORKSPACE_ROOT, init_reporter)
    content_first = fs.files[WORKSPACE_ROOT / AGENTS_MD_FILENAME]

    ok2 = svc.generate(WORKSPACE_ROOT, init_reporter)
    content_second = fs.files[WORKSPACE_ROOT / AGENTS_MD_FILENAME]

    assert ok1 is True
    assert ok2 is True
    assert content_first == content_second


def test_generate_no_unresolved_at_imports(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """The generated AGENTS.md must not contain unresolved @path file references.

    After full resolution, any remaining @word token must not be a file path (i.e.
    must not contain '/' or '.'), meaning all @imports have been inlined or replaced
    with comments.
    """
    import re

    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "Hello @ai/project/index.md and @CLAUDE.winter.md.\n",
            WORKSPACE_ROOT / "ai" / "project" / "index.md": "# Project\n",
            WORKSPACE_ROOT / "CLAUDE.winter.md": "# Winter extensions\n",
        }
    )
    svc = AgentsMdService(config=workspace_config, fs=fs)

    svc.generate(WORKSPACE_ROOT, init_reporter)
    content = fs.files[WORKSPACE_ROOT / AGENTS_MD_FILENAME]

    _IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s`]+)")
    _TRIM = ".,;:!?)]}>\"'"

    unresolved = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _IMPORT_RE.finditer(line):
            raw = m.group(1).rstrip(_TRIM)
            if "/" in raw or "." in raw:
                unresolved.append(raw)

    assert unresolved == [], f"Found unresolved @imports: {unresolved}"


def test_generate_updates_on_source_change(
    workspace_config: WorkspaceConfig, init_reporter: FakeInitReporter
) -> None:
    """generate() rewrites AGENTS.md when a source file changes."""
    fs = FakeFilesystem(
        files={
            WORKSPACE_ROOT / "CLAUDE.md": "@ai/index.md\n",
            WORKSPACE_ROOT / "ai" / "index.md": "Original content\n",
        }
    )
    svc = AgentsMdService(config=workspace_config, fs=fs)

    svc.generate(WORKSPACE_ROOT, init_reporter)
    first_content = fs.files[WORKSPACE_ROOT / AGENTS_MD_FILENAME]

    # Mutate the source.
    fs.files[WORKSPACE_ROOT / "ai" / "index.md"] = "Changed content\n"
    svc.generate(WORKSPACE_ROOT, init_reporter)
    second_content = fs.files[WORKSPACE_ROOT / AGENTS_MD_FILENAME]

    assert first_content != second_content
    assert "Changed content" in second_content
