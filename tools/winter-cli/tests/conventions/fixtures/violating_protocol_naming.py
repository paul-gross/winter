"""Deliberately violates `winter-harness:/architecture/module-layout.md`.

Excluded from pytest collection via `conftest.collect_ignore`.
`test_protocol_naming.py` imports this file's AST directly to confirm the
detection actually fires — regression coverage on the lint itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class BadProtocol(Protocol):
    """No I-prefix — should be flagged."""

    def do_something(self) -> None: ...


@runtime_checkable
class AlsoBad(Protocol):
    """Also no I-prefix and decorated — should also be flagged."""

    def call(self) -> None: ...
