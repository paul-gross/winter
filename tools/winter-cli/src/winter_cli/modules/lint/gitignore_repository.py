from __future__ import annotations

from pathlib import Path
from typing import Protocol


class IGitIgnoreRepository(Protocol):
    """Resolves which of a scope root's candidate paths its owning repo gitignores."""

    def filter_ignored(self, scope_root: Path, candidates: list[Path]) -> list[Path]: ...
