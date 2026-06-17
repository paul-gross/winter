from __future__ import annotations

from typing import Any

# Keys whose list values are TOML array-of-tables: the overlay appends to the
# base rather than replacing it. Every other list key (e.g. `env_aliases`,
# `git_excludes`, `lint`) is a scalar list that the overlay can replace outright.
_CONCAT_LIST_KEYS: frozenset[str] = frozenset({"project_repository", "standalone_repository"})


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Overlay scalar keys win; dicts recurse.

    List handling is key-aware:
    - ``project_repository`` and ``standalone_repository`` (TOML array-of-tables)
      are concatenated so ``config.local.toml`` can add entries without wiping
      the shared set declared in ``config.toml``.
    - All other list keys (``env_aliases``, ``git_excludes``, ``lint``, …) are
      replaced by the overlay value, allowing ``config.local.toml`` to trim or
      rewrite them entirely.
    """
    if not overlay:
        return dict(base)
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            result[key] = deep_merge(existing, value)
        elif isinstance(value, list) and isinstance(existing, list) and key in _CONCAT_LIST_KEYS:
            result[key] = existing + value
        else:
            result[key] = value
    return result
