"""Deliberately violates `winter-harness:/architecture/dependency-injection.md`.

Excluded from pytest collection via `conftest.collect_ignore`.
`test_no_whole_config_injection.py` imports this file's AST directly to
confirm the detection actually fires.
"""

from __future__ import annotations


class WorkspaceConfig:
    """Stand-in for the real type — the AST check is name-based."""


class TaggingService:
    def __init__(self, config: WorkspaceConfig) -> None:
        # Reaches into the app config schema — should be a typed Workspace
        # domain object or a small dataclass.
        self._config = config
