"""Convention test — no catch-log-rethrow except at entrypoints.

Convention: `winter-harness:/architecture/error-handling.md`.

The anti-pattern is an `except` handler that both logs the caught
exception AND re-raises — every layer in the call stack ends up logging
the same error before the actual handler at the boundary surfaces it,
producing duplicate noise without any actual handling.

Detection walks every `Try` node; for each handler, two flags:

- "logs the exception" — a `Call` recognizable as a logging call at the
  **top level** of the handler body. The recognized shapes are
  `logger.<level>(...)`, `traceback.<fn>(...)`, `print(...)`, and any
  attribute chain ending in `.<level>` where `<level>` ∈ `LOGGING_ATTRS`
  (catches `self._logger.error(...)` and friends). The last shape is
  intentionally permissive — false positives would be a non-logger
  object with an `error()` / `warning()` / `exception()` / `critical()`
  method that happens to be called inside a re-raising except handler.
  None exist in the current codebase (verified by grep).
- "re-raises" — a bare `raise` statement (no value), or `raise <var>`
  naming the captured exception, at the **top level** of the handler body.

Two known blind spots:

- **Mutually-exclusive `if/else` branches.** When the log call lives in
  one arm and the raise in the other, only one runs per call. Not the
  anti-pattern. Not flagged.
- **Nested control flow.** When both the log and the raise live inside
  the same `if`/`for`/`with`, both run on that path and the anti-pattern
  applies, but this implementation only scans top-level statements so
  it doesn't catch it. Trade-off: simpler implementation, zero false
  positives from `if/else` recovery patterns. Revisit if a violation in
  this shape lands.

Files under `ENTRYPOINT_ALLOWLIST` (CLI entrypoints, matched by path
relative to `src/winter_cli/`) are exempt: that *is* the boundary that
surfaces the error, and the convention permits log-and-exit at that
layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conventions.conftest import SRC_ROOT, location, walk_src

CONVENTION_DOC = "winter-harness:/architecture/error-handling.md"


# CLI entrypoints exempt from this rule. Matched against the path
# relative to `src/winter_cli/`, so only the package-root entrypoint
# qualifies — a future `modules/foo/cli.py` would NOT be exempted.
ENTRYPOINT_ALLOWLIST = frozenset(
    {
        "cli.py",
        "__main__.py",
    }
)

LOGGING_ATTRS = frozenset({"exception", "error", "warning", "critical"})


def _is_logging_call(node: ast.AST) -> bool:
    """Statement-level call that looks like logging — see module docstring."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    if isinstance(func, ast.Attribute):
        # `<obj>.<attr>(...)` — looks like logger.<level> or traceback.<fn>.
        if isinstance(func.value, ast.Name):
            if func.value.id == "logger" and func.attr in LOGGING_ATTRS:
                return True
            if func.value.id == "traceback":
                return True
        # `self._logger.error(...)` or similar.
        if func.attr in LOGGING_ATTRS and (
            isinstance(func.value, ast.Attribute)
            or (isinstance(func.value, ast.Name) and "log" in func.value.id.lower())
        ):
            return True
    return isinstance(func, ast.Name) and func.id == "print"


def _is_reraise(node: ast.AST, exc_name: str | None) -> bool:
    """Bare `raise`, or `raise <exc_name>` matching the captured exception var."""
    if not isinstance(node, ast.Raise):
        return False
    if node.exc is None:
        return True
    return exc_name is not None and isinstance(node.exc, ast.Name) and node.exc.id == exc_name


def _relative_module(file_path: Path) -> str | None:
    """Return the path relative to `src/winter_cli/`, or None for out-of-tree files."""
    try:
        return file_path.relative_to(SRC_ROOT).as_posix()
    except ValueError:
        return None


def find_catch_log_rethrow_violations(file_path: Path, tree: ast.Module) -> list[str]:
    rel = _relative_module(file_path)
    if rel is not None and rel in ENTRYPOINT_ALLOWLIST:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc_name = handler.name
            top_level = handler.body
            logs = any(_is_logging_call(stmt) for stmt in top_level)
            reraises = any(_is_reraise(stmt, exc_name) for stmt in top_level)
            if logs and reraises:
                violations.append(
                    f"{location(file_path, handler)}: except handler logs and re-raises "
                    f"the same exception ({CONVENTION_DOC})"
                )
    return violations


def test_no_catch_log_rethrow_outside_entrypoints() -> None:
    all_violations: list[str] = []
    for path, tree in walk_src():
        all_violations.extend(find_catch_log_rethrow_violations(path, tree))
    if all_violations:
        pytest.fail("\n".join(["Catch-log-rethrow violations:", *all_violations]))


def test_fixture_violation_is_detected() -> None:
    fixture = Path(__file__).parent / "fixtures" / "violating_no_catch_log_rethrow.py"
    tree = ast.parse(fixture.read_text(encoding="utf-8"), filename=str(fixture))
    violations = find_catch_log_rethrow_violations(fixture, tree)
    assert violations, "fixture must trigger at least one violation"
    assert any("logs and re-raises" in v for v in violations)
