"""Parser for the winter service describe document.

The orchestrator emits a JSON object on stdout for the ``describe`` action.  Winter
parses that object into a frozen dataclass (shape-stability: missing or invalid
fields receive safe defaults).

Schema::

    {"services": ["name", ...]}

``services`` must be a list of strings; unknown/empty → ``()``.  Malformed JSON or a
non-object top level raises ``DescribeParseError`` with an actionable message that
names the provider.
"""

from __future__ import annotations

import json

from winter_cli.modules.service.describe_models import DescribeResult


class DescribeParseError(Exception):
    """Raised when the provider's describe output cannot be parsed.

    The message is human-readable and actionable — it names the provider and
    describes the specific non-conformance so the operator knows where to look.
    """


class DescribeResultParser:
    """Owns parsing the provider's describe stdout into a ``DescribeResult``."""

    def parse(self, raw_stdout: str, *, provider_name: str) -> DescribeResult:
        """Parse the provider's stdout into a typed ``DescribeResult``.

        Applies shape-stability defaults: a missing or non-list ``services`` key
        yields an empty tuple.  ``{"services": []}`` is a valid, non-error response.

        Raises ``DescribeParseError`` on hard non-conformance: malformed JSON or a
        top-level value that is not a dict.
        """
        try:
            obj = json.loads(raw_stdout)
        except json.JSONDecodeError as exc:
            raise DescribeParseError(
                f"provider {provider_name!r} did not emit a valid JSON describe document: {exc}"
            ) from exc

        if not isinstance(obj, dict):
            raise DescribeParseError(
                f"provider {provider_name!r} describe document must be a JSON object at the top level,"
                f" got {type(obj).__name__}"
            )

        raw_services = obj.get("services")
        if not isinstance(raw_services, list):
            # Missing or non-list key → empty tuple (shape-stability).
            return DescribeResult(services=())

        services = tuple(s for s in raw_services if isinstance(s, str))
        return DescribeResult(services=services)
