"""Shared helpers for provider invocation: env-dict construction and pattern matching.

``build_provider_env`` builds the WINTER_* environment dict for any provider
subprocess call, merging the current process environment with the three
workspace context variables.

``service_matches_pattern`` is the segment-aware fnmatch check used by
``restart`` and ``logs`` routing to decide whether a known service name
matches a user-supplied selection pattern.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any


def build_provider_env(provider: Any, workspace_root: Path) -> dict[str, str]:
    """Return a copy of os.environ with WINTER_WORKSPACE_DIR/EXT_DIR/EXT_PREFIX set.

    ``provider`` must expose ``ext_dir: Path`` and ``prefix: str``; compatible
    with both ``ResolvedCapability`` and ``ResolvedOrchestrator``.
    """
    merged = os.environ.copy()
    merged["WINTER_WORKSPACE_DIR"] = str(workspace_root)
    merged["WINTER_EXT_DIR"] = str(provider.ext_dir)
    merged["WINTER_EXT_PREFIX"] = provider.prefix
    return merged


def service_matches_pattern(svc_name: str, pattern: str) -> bool:
    """Return True when ``svc_name`` matches ``pattern``.

    Handles two forms:
    - Two-segment ``<env>/<svc>`` pattern: only the svc segment is matched
      against ``svc_name`` (the env segment is used for env-scoping at the
      provider level — see dispatch routing).
    - Bare pattern (no ``/``): matched directly against ``svc_name`` via fnmatch.
    """
    if "/" in pattern:
        _env_seg, svc_seg = pattern.split("/", 1)
        return fnmatch.fnmatchcase(svc_name, svc_seg)
    return fnmatch.fnmatchcase(svc_name, pattern)
