"""Convention test — Protocols are I-prefixed and live outside internal/.

Convention: `winter-harness:/architecture/module-layout.md`,
`winter-harness:/standards/protocol-conformance.md`.

Walks every `.py` under `src/winter_cli/` and inspects each `ClassDef`.
A class is a Protocol if either:
- one of its bases is `Protocol` (typing.Protocol), OR
- it carries the `@runtime_checkable` decorator.

For each Protocol class, two assertions:
1. The class name starts with `I` (e.g. `IFooRepository`, not `FooRepository`).
2. The defining file is not under an `internal/` directory — internal/ holds
   adapters, the Protocol seam lives at the feature root.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conventions.conftest import location, walk_src

CONVENTION_DOC = "winter-harness:/architecture/module-layout.md"


def _is_protocol_class(node: ast.ClassDef) -> bool:
    """True iff the class is a Protocol — by base or @runtime_checkable decorator."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "Protocol":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "Protocol":
            return True
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Name) and target.id == "runtime_checkable":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "runtime_checkable":
            return True
    return False


def find_protocol_violations(file_path: Path, tree: ast.Module) -> list[str]:
    """Return one failure line per Protocol-rule violation in this file."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_protocol_class(node):
            continue
        if not node.name.startswith("I"):
            violations.append(
                f"{location(file_path, node)}: Protocol {node.name!r} must start with 'I' ({CONVENTION_DOC})"
            )
        if "internal" in file_path.parts:
            violations.append(
                f"{location(file_path, node)}: Protocol {node.name!r} cannot live in internal/ "
                f"(adapters do — {CONVENTION_DOC})"
            )
    return violations


def test_all_protocols_follow_naming_and_layout_rules() -> None:
    all_violations: list[str] = []
    for path, tree in walk_src():
        all_violations.extend(find_protocol_violations(path, tree))
    if all_violations:
        pytest.fail("\n".join(["Protocol convention violations:", *all_violations]))


def test_fixture_violations_are_detected() -> None:
    """Regression — the deliberate violation in fixtures/ must trigger."""
    fixture = Path(__file__).parent / "fixtures" / "violating_protocol_naming.py"
    tree = ast.parse(fixture.read_text(encoding="utf-8"), filename=str(fixture))
    violations = find_protocol_violations(fixture, tree)
    assert violations, "fixture must trigger at least one violation"
    assert any("must start with 'I'" in v for v in violations)
