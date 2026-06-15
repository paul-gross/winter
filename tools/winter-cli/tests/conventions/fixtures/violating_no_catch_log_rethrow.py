"""Deliberately violates `winter-harness:/architecture/error-handling.md`.

Excluded from pytest collection via `conftest.collect_ignore`.
`test_no_catch_log_rethrow.py` imports this file's AST directly to
confirm the detection actually fires.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def do_step() -> None:
    try:
        _inner()
    except ValueError as exc:
        # Catch-log-rethrow — every layer logs the same error then bubbles it.
        logger.error("something failed: %s", exc)
        raise


def _inner() -> None:
    raise ValueError("boom")
