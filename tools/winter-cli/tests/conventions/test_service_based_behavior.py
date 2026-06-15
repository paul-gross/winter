"""Convention test — behavior lives in service classes, not free functions.

Convention: `winter-harness:/architecture/service-architecture.md`.

Winter code is service-based: behavior lives in injected service classes
behind Protocol seams; module-level free functions are reserved for pure,
dependency-free helpers. The tractable, false-positive-free signal of a
violation is a **module-level function whose parameter is annotated with an
`I`-prefixed Protocol** — the form every injected collaborator takes. DI
consumers depend on the Protocol seam, never the concrete adapter
(`architecture/dependency-injection.md`), and the `I`-prefix is reserved for
Protocols by the naming check (`architecture/module-layout.md`). So an
`I`-prefixed parameter type is unambiguously a collaborator seam, and a free
function receiving one is behavior that escaped its class. A pure helper
takes only plain values (stdlib types, domain dataclasses).

Why match the `I`-prefix rather than concrete role suffixes
(`*Service`, `*Repository`, …): domain dataclasses share those nouns —
`ProjectRepository` is a value, not a collaborator — so a suffix match would
flag the pure helpers the convention explicitly permits. The `I`-prefix
namespace is reserved for Protocols, so matching it carries no such
collision. The `_conforms_*` conformance sentinels
(`standards/protocol-conformance.md`) take a *concrete* adapter, not an `I*`
Protocol, so they fall outside this signal automatically — no carve-out
needed.

Scope and blind spots:

- **Module-level functions only.** Methods (functions nested inside a
  `ClassDef`) are skipped — a method taking a collaborator is exactly the
  service shape we want. Iterating `tree.body` reaches only module-level
  statements, so methods are never visited.
- **Protocol-param signal only.** A free function that takes a *concrete*
  collaborator, or *constructs* its collaborators inside its body, is not
  caught here — the former is also covered by the I-prefix naming rule, the
  latter is `architecture/repository-pattern.md`'s territory (no library imports
  outside repository classes). Catching either structurally would trade the
  false-positive-free guarantee for marginal coverage; this check stays on
  the precise Protocol-param signal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conventions.conftest import location, walk_src

CONVENTION_DOC = "winter-harness:/architecture/service-architecture.md"


def _annotation_name(node: ast.expr | None) -> str | None:
    """Best-effort extraction of the simple name from a type annotation."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    return None


def _is_protocol_seam(name: str | None) -> bool:
    """True iff the annotation names an `I`-prefixed Protocol (e.g. `IReadFooRepository`)."""
    return bool(name) and name.startswith("I") and len(name) > 1 and name[1].isupper()


def _iter_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = func.args
    params: list[ast.arg] = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        params.append(args.vararg)
    if args.kwarg is not None:
        params.append(args.kwarg)
    return params


def find_service_based_violations(file_path: Path, tree: ast.Module) -> list[str]:
    """One failure line per module-level free function taking a Protocol seam."""
    violations: list[str] = []
    # tree.body holds module-level statements only — methods (nested in a
    # ClassDef) are never visited, which is the intended scoping.
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for arg in _iter_params(node):
            name = _annotation_name(arg.annotation)
            if _is_protocol_seam(name):
                violations.append(
                    f"{location(file_path, arg)}: free function {node.name!r} takes "
                    f"`{arg.arg}: {name}` — a Protocol collaborator belongs on a service "
                    f"class, not a module-level function ({CONVENTION_DOC})"
                )
    return violations


def test_all_behavior_lives_in_service_classes() -> None:
    all_violations: list[str] = []
    for path, tree in walk_src():
        all_violations.extend(find_service_based_violations(path, tree))
    if all_violations:
        pytest.fail("\n".join(["Service-based architecture violations:", *all_violations]))


def test_fixture_violation_is_detected() -> None:
    fixture = Path(__file__).parent / "fixtures" / "violating_service_based_behavior.py"
    tree = ast.parse(fixture.read_text(encoding="utf-8"), filename=str(fixture))
    violations = find_service_based_violations(fixture, tree)
    assert violations, "fixture must trigger at least one violation"
    assert any("IWriteFooRepository" in v for v in violations)
    assert not any("normalize" in v for v in violations), "pure helper must not be flagged"
    assert not any("describe" in v for v in violations), "domain-dataclass param must not be flagged"
