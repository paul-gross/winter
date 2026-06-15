"""Shared helpers for convention tests.

Each test in `tests/conventions/` walks `src/winter_cli/` with `ast` and
asserts a convention from `winter-harness:/architecture/*.md` or `winter-harness:/standards/*.md`. The helper here
provides the common file → AST iteration so every rule file stays small.

`fixtures/` contains files that deliberately violate the rules. They are
excluded from pytest collection via `collect_ignore` so the suite stays
green; the per-rule test files import them explicitly and verify the
detection fires against them (regression-tests-on-the-lint).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

# pytest reads this module-level attribute and skips collection for these
# paths. Fixture files are import-time syntactically valid but deliberately
# violate the conventions these tests enforce; without this they'd surface
# as suite failures.
collect_ignore = ["fixtures"]


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "winter_cli"


def walk_src() -> Iterator[tuple[Path, ast.Module]]:
    """Yield (file_path, parsed_module) for every `.py` under `src/winter_cli/`.

    Used by every test in this directory. Skips `__pycache__`. A
    syntactically-broken source file will surface as a `SyntaxError`
    from `ast.parse` and fail the test that's iterating — pyright is
    the proper gate for syntax, but a convention test crashing is a
    louder signal than silently skipping the file.
    """
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        yield path, tree


def location(file_path: Path, node: ast.AST) -> str:
    """Format a `file:line` reference for failure messages."""
    return f"{file_path}:{getattr(node, 'lineno', '?')}"
