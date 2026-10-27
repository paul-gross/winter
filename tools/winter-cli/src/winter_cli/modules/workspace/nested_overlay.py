"""Pure arithmetic for the keys winter delegates into a nested workspace's local overlay."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from winter_cli.config.models import WorkspaceConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_DEFAULT_PORTS_PER_ENV = WorkspaceConfig.model_fields["ports_per_env"].default
_DEFAULT_ENVS_PER_WORKSPACE = WorkspaceConfig.model_fields["envs_per_workspace"].default


def delegated_keys(port_base: int, service_prefix: str, envs: int | None) -> dict[str, Any]:
    """The overlay keys winter writes into the nested workspace for one outer env.

    `base_port` and `service_prefix` always. With *envs* (the usable nested
    feature-env count) also `env_aliases = []` and `envs_per_workspace = envs +
    1`: with no aliases, index 1 is the reserved buffer slot, so `envs` usable
    indices need `envs + 1` slots.
    """
    keys: dict[str, Any] = {"base_port": port_base, "service_prefix": service_prefix}
    if envs is not None:
        keys["env_aliases"] = []
        keys["envs_per_workspace"] = envs + 1
    return keys


def slice_size(band: int, nested_count: int) -> int:
    """Ports each of the `nested_count + 1` equal slices of an outer env's *band* holds; the remainder is unused."""
    return band // (nested_count + 1)


def slice_base(env_port_base: int, size: int, position: int) -> int:
    """First port of the slice at *position* (1-based, in configuration order); slice 0 is the parent's own."""
    return env_port_base + position * size


REPOSITORY_ARRAYS = ("project_repository", "standalone_repository")
"""Overlay keys that declare repositories; never inherited, even when named."""


def inherited_keys(outer_local: Mapping[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """The named top-level keys and tables of the outer workspace's raw local overlay.

    *outer_local* is the outer `config.local.toml` as written, not the merged
    config, so a key from the committed config or a default is never passed
    down. A name the overlay does not hold is skipped, and the repository
    arrays are skipped even when named. Values are copied, so the nested
    write cannot alias the outer overlay.
    """
    return {
        name: copy.deepcopy(outer_local[name])
        for name in names
        if name in outer_local and name not in REPOSITORY_ARRAYS
    }


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def effective_ports_per_env(committed: Mapping[str, Any], local: Mapping[str, Any]) -> int:
    """The nested workspace's `ports_per_env` after the overlay, at winter's default when unset."""
    return _int(local.get("ports_per_env", committed.get("ports_per_env")), _DEFAULT_PORTS_PER_ENV)


def effective_envs_per_workspace(committed: Mapping[str, Any], local: Mapping[str, Any]) -> int:
    """The nested workspace's `envs_per_workspace` after the overlay, at winter's default when unset."""
    return _int(local.get("envs_per_workspace", committed.get("envs_per_workspace")), _DEFAULT_ENVS_PER_WORKSPACE)


def footprint(committed: Mapping[str, Any], local_after: Mapping[str, Any]) -> int:
    """Ports the nested workspace can use: `(envs_per_workspace + 1) * ports_per_env`.

    Indices run `0..envs_per_workspace`, each owning `ports_per_env` ports.
    *committed* is the nested `config.toml`; *local_after* is its
    `config.local.toml` with the delegated keys applied, which wins.
    """
    return (effective_envs_per_workspace(committed, local_after) + 1) * effective_ports_per_env(committed, local_after)
