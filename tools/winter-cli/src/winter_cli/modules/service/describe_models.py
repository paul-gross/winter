"""Typed data models for the winter service describe document.

Parsing lives in ``describe_parser.py`` (``DescribeResultParser``).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class DescribeResult:
    """Result of the ``describe`` action from one provider.

    ``services`` is the tuple of service names the provider declares ownership of.
    An unknown or empty ``services`` key in the provider's JSON response yields ``()``.
    """

    services: tuple[str, ...]
