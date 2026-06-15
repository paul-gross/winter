"""Deliberately violates `winter-harness:/architecture/service-architecture.md`.

Excluded from pytest collection via `conftest.collect_ignore`.
`test_service_based_behavior.py` imports this file's AST directly to
confirm the detection actually fires — regression coverage on the lint.
"""

from __future__ import annotations


class IWriteFooRepository:
    """Stand-in for a Protocol seam — the AST check is name-based."""

    def pull(self) -> None: ...


class ProjectRepository:
    """Stand-in for a domain dataclass — a value, not a collaborator."""

    name: str


def sync(foo_repo: IWriteFooRepository) -> None:
    # Behavior reaching for an injected Protocol collaborator at module
    # scope — should be a method on a service class, not a free function.
    foo_repo.pull()


def describe(repo: ProjectRepository) -> str:
    # Takes a domain dataclass, not a Protocol seam — a legitimate pure
    # helper. Must NOT be flagged, even though the type ends in "Repository".
    return repo.name


def normalize(name: str) -> str:
    # Pure helper, takes only stdlib types — legitimately a free function,
    # must NOT be flagged.
    return name.strip().lower()
