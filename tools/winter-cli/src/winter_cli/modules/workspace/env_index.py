from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from winter_cli.config.models import _DEFAULT_ENV_ALIASES
from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry

if TYPE_CHECKING:
    from winter_cli.config.models import WorkspaceConfig

GREEK_LETTERS = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "omicron",
    "pi",
    "rho",
    "sigma",
    "tau",
    "upsilon",
    "phi",
    "chi",
    "psi",
    "omega",
]

# Fallback alias list used by resolve_env_index when no config is available.
# Imported from config/models.py (first 10 Greek letters) — single source of truth.
# Used only by: (a) the read-path fallback for pre-registry envs that lack a
# state.toml entry, and (b) `winter ws index`. Init always receives real config
# via EnvIndexAllocator.allocate.
_DEFAULT_ALIASES = _DEFAULT_ENV_ALIASES
_DEFAULT_ENVS_PER_WORKSPACE = 48


def is_valid_env_index(idx: int, env_aliases: list[str], envs_per_workspace: int) -> bool:
    """Return ``True`` when *idx* is a legitimately allocatable index.

    Valid indices are those the allocator can assign:

    * ``1..N`` — alias slots (one per entry in *env_aliases*).
    * ``N+2..envs_per_workspace`` — the hash band (inclusive on both ends).

    The reserved index ``0`` and the buffer slot ``N+1`` are **not** valid;
    neither is anything above *envs_per_workspace*.  This is the single source
    of truth for range validation — the doctor probe delegates here rather than
    re-encoding the bounds itself.
    """
    n = len(env_aliases)
    if idx == 0:
        return False
    if 1 <= idx <= n:
        return True
    if idx == n + 1:
        # Buffer slot: reserved, never assigned by the allocator.
        return False
    return n + 2 <= idx <= envs_per_workspace


def resolve_env_index(
    name: str,
    env_aliases: list[str] | None = None,
    envs_per_workspace: int | None = None,
) -> int:
    """Return the *suggested* index for *name* (does not consult the registry).

    The index layout is driven by *env_aliases* and *envs_per_workspace*:

    * Aliases (``env_aliases[0..N-1]``) get fixed indices ``1..N``
      by list position.
    * Index ``N+1`` is a buffer slot (reserved, never assigned).
    * All other names hash into the band ``N+2 .. envs_per_workspace`` via
      SHA-1, giving a deterministic *suggestion* that may still collide with
      another env.  The allocator (``EnvIndexAllocator.allocate``) resolves
      collisions; this function only suggests.

    When *env_aliases* / *envs_per_workspace* are ``None`` the function uses
    the default alias list (first 10 Greek letters) and 48 envs-per-workspace.
    This fallback is used by the read-path for pre-registry envs and by
    ``winter ws index``.

    Index 0 is reserved and will never be returned.  It is earmarked for a
    future single-slot "local" environment — a pre-seeded shared dataset /
    workspace area — distinct in purpose from the ``N+1`` buffer slot between
    aliases and the hash band.
    """
    aliases = env_aliases if env_aliases is not None else _DEFAULT_ALIASES
    n_envs = envs_per_workspace if envs_per_workspace is not None else _DEFAULT_ENVS_PER_WORKSPACE
    return _resolve_with_params(name, aliases, n_envs)


def _resolve_with_params(name: str, env_aliases: list[str], envs_per_workspace: int) -> int:
    """Core index computation against explicit alias list and slot count."""
    alias_index = {alias: i + 1 for i, alias in enumerate(env_aliases)}

    if name in alias_index:
        return alias_index[name]

    n = len(env_aliases)
    # hash band: N+2 .. envs_per_workspace (inclusive)
    hash_bucket = envs_per_workspace - n - 1
    if hash_bucket <= 0:
        # Config validation already guarantees envs_per_workspace >= len(env_aliases) + 2,
        # so hash_bucket >= 1 in all valid configs.  Reaching here signals a
        # misconfiguration that slipped past validation — raise rather than
        # silently returning a nonsense index that would shadow the root cause.
        raise ValueError(
            f"hash band is empty: envs_per_workspace={envs_per_workspace} must be "
            f"at least len(env_aliases)+2={n + 2}; "
            f"check your .winter/config.toml"
        )
    digest = hashlib.sha1(name.encode()).digest()
    offset = int.from_bytes(digest[:2], "big") % hash_bucket
    return (n + 2) + offset


def build_env_trio(
    env_name: str,
    config: WorkspaceConfig,
    registry: IEnvIndexRegistry | None,
) -> dict[str, str]:
    """Return ``WINTER_ENV``/``WINTER_ENV_INDEX``/``WINTER_PORT_BASE`` for *env_name*.

    Resolves the index registry-first (so the value agrees with the
    ``.winter.env`` that ``init_service`` already wrote), falling back to the
    config-aware formula for envs not yet recorded (pre-init hooks or
    pre-registry environments).
    """
    index = registry.get_index(env_name) if registry is not None else None
    if index is None:
        index = resolve_env_index(
            env_name,
            config.env_aliases,
            config.envs_per_workspace,
        )
    return {
        "WINTER_ENV": env_name,
        "WINTER_ENV_INDEX": str(index),
        "WINTER_PORT_BASE": str(config.port_base_for_index(index)),
    }


class EnvIndexAllocator:
    """Allocates stable, collision-free env indices, persisting them to the registry.

    Holds the ``IEnvIndexRegistry`` collaborator; callers pass plain config
    values (``env_aliases``, ``envs_per_workspace``) to :meth:`allocate` so the
    allocator never depends on the whole ``WorkspaceConfig`` object.
    """

    def __init__(self, registry: IEnvIndexRegistry) -> None:
        self._registry = registry

    def allocate(self, name: str, env_aliases: list[str], envs_per_workspace: int) -> int:
        """Allocate a stable, collision-free index for *name* and persist it.

        Allocation rules:

        * If *name* is a configured alias → its fixed slot ``1..N``.  Aliases are
          unique by definition so no probing is needed.  The result is written to
          the registry (idempotent if already present).
        * If *name* is already registered → return the recorded index unchanged
          (idempotent re-allocation).
        * Otherwise: compute the suggested hash slot.  If that slot is already
          occupied by a *different* name, linear-probe upward within the hash
          band (``N+2..envs_per_workspace``), wrapping around within the band.
          Raises ``IndexError`` when the entire hash band is full.

        Index 0 is reserved and is never returned.
        """
        registry = self._registry
        n = len(env_aliases)
        alias_index = {alias: i + 1 for i, alias in enumerate(env_aliases)}

        # --- alias path ---
        if name in alias_index:
            fixed = alias_index[name]
            registry.assign(name, fixed)
            return fixed

        # --- idempotent: already registered ---
        existing = registry.get_index(name)
        if existing is not None:
            return existing

        # --- hash + probe ---
        suggested = _resolve_with_params(name, env_aliases, envs_per_workspace)
        assignments = registry.all_assignments()
        # Invert: index → name (ignore this env's own entry, which doesn't exist yet)
        occupied: dict[int, str] = {idx: n_key for n_key, idx in assignments.items()}

        hash_band_start = n + 2
        hash_band_end = envs_per_workspace  # inclusive
        hash_bucket = hash_band_end - hash_band_start + 1

        if hash_bucket <= 0:
            raise IndexError(
                f"Cannot allocate index for env {name!r}: hash band is empty "
                f"(envs_per_workspace={envs_per_workspace}, aliases={n})"
            )

        candidate = suggested
        for _ in range(hash_bucket):
            owner = occupied.get(candidate)
            if owner is None or owner == name:
                # Free slot found.
                registry.assign(name, candidate)
                return candidate
            # Probe: advance within the hash band, wrapping around.
            candidate = hash_band_start + (candidate - hash_band_start + 1) % hash_bucket

        raise IndexError(
            f"Cannot allocate index for env {name!r}: all {hash_bucket} slots in the "
            f"hash band ({hash_band_start}..{hash_band_end}) are occupied"
        )
