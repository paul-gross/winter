"""Single source of truth for computing the runtime environment map for a scope.

``EnvProvisionerService.compute(scope, resolve_commands=...)`` returns the
complete ``{KEY: VALUE}`` env map that winter injects into every provider
subprocess and that ``winter env`` prints as sourceable lines.  All callers —
``winter env``, ``ServiceFanOutService`` (up/down), ``ServiceStatusMatrixService``
(status) — delegate here so the computation is never duplicated.

``resolve_commands`` is keyword-only with no default: each call site owns a
policy decision about whether a command entry's output is actually resolved
(``True``) or masked to a placeholder (``False``), and ``compute`` merely
forwards the caller's choice to ``EnvBandResolverService.resolve()`` — it makes
no gating decision of its own.

Scope semantics
---------------
*scope* is either a feature-env name (e.g. ``"alpha"``) or the reserved literal
``"workspace"``.  The workspace scope uses index 0 (reserved, never allocated to
a feature env); its port base is therefore ``config.port_base_for_index(0)``.

Rendered vars
-------------
The returned map always contains:

    WINTER_ENV                  — scope name
    WINTER_ENV_INDEX            — allocated index as a decimal string
    WINTER_WORKSPACE_PORT_BASE  — port-band start for index 0
    WINTER_SERVICE_PREFIX       — resolved workspace service-namespace prefix

For a feature-env scope, additionally:

    WINTER_PORT_BASE            — port-band start for this scope's own band

For the ``"workspace"`` scope, ``WINTER_PORT_BASE`` is deliberately NOT emitted.
The workspace band is exposed ONLY as ``WINTER_WORKSPACE_PORT_BASE`` so the name
carries one meaning everywhere (the per-env band); emitting it under the
workspace value (index-0) would make the name ambiguous across scopes.

Band selection
--------------
``[env.workspace.vars]``, ``[env.feature.vars]``, and the per-env
``[env.<name>.vars]`` override tables are selected by scope:

- **workspace scope**: only ``[env.workspace.vars]`` entries are visible to
  resolution.
- **feature scope**: ``[env.workspace.vars]``, ``[env.feature.vars]``, and this
  env's own ``[env.<name>.vars]`` band are all visible together.

Only the band matching the scope's own name is selected, so a per-env override
never reaches a sibling env, and one naming an env that does not exist is
simply never looked up — inert, not an error.

This service's job stops at selecting which bands apply and computing the base
vars above; turning the selected bands into concrete values is
``EnvBandResolverService.resolve()``'s job (see
``modules/workspace/env_band_resolver_service.py``), which resolves every
entry to a fixpoint rather than in a single ordered pass — a band entry may
reference any other band's entry regardless of declaration order, with one
exception: a workspace-band entry only ever sees the base vars — as originally
computed here, never as any band entry (including the workspace band's own)
may have since rewritten them — minus ``WINTER_PORT_BASE``, plus other
workspace-band keys, so it resolves identically at every scope even when a
feature-, named-, or the workspace band itself happens to declare a key
sharing a base var's name. See that module's docstring for the
full fixpoint contract, including how a key declared in a higher band shadows
a lower band's entry for that key entirely, and how an unresolvable entry is
diagnosed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from winter_cli.modules.service.scope import WORKSPACE_SCOPE
from winter_cli.modules.workspace.env_index import build_env_trio

if TYPE_CHECKING:
    from winter_cli.config.models import WorkspaceConfig
    from winter_cli.modules.workspace.env_band_resolver_service import EnvBandResolverService
    from winter_cli.modules.workspace.env_index_registry import IEnvIndexRegistry


class EnvProvisionerService:
    """Compute the full runtime environment map for any scope.

    The map is the authoritative set of ``WINTER_*`` variables that winter
    injects into provider subprocesses and that ``winter env`` prints.
    Call :meth:`compute` with a feature-env name or ``"workspace"`` to get the
    complete ``{KEY: VALUE}`` dict.

    Construction::

        EnvProvisionerService(config, registry, band_resolver)
    """

    def __init__(
        self,
        config: WorkspaceConfig,
        registry: IEnvIndexRegistry,
        band_resolver: EnvBandResolverService,
    ) -> None:
        self._config = config
        self._registry = registry
        self._band_resolver = band_resolver

    def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
        """Return the full env map for *scope*.

        For a feature env this is the env trio (``WINTER_ENV``,
        ``WINTER_ENV_INDEX``, ``WINTER_PORT_BASE``) plus
        ``WINTER_WORKSPACE_PORT_BASE`` and every resolved band entry.

        For ``"workspace"``, ``WINTER_ENV``, ``WINTER_ENV_INDEX``, and
        ``WINTER_WORKSPACE_PORT_BASE`` are returned (index 0, the workspace port
        base) plus ``[env.workspace.vars]`` entries only.  ``WINTER_PORT_BASE``
        is deliberately NOT included for the workspace scope — the workspace band
        is exposed only as ``WINTER_WORKSPACE_PORT_BASE`` so the name carries one
        meaning everywhere.

        Band selection by scope:

        - workspace scope: ``[env.workspace.vars]`` entries only.
        - feature scope: ``[env.workspace.vars]``, ``[env.feature.vars]``, and
          this env's own ``[env.<name>.vars]`` band, all resolved together —
          see ``EnvBandResolverService`` for how entries across bands may
          reference one another.

        *resolve_commands* gates command-entry execution: ``True`` runs each
        command entry (once its references resolve) and merges its output;
        ``False`` masks every command-derived key to the resolver's
        placeholder without running anything. See
        ``EnvBandResolverService.resolve()`` for the full gate contract.

        Raises ``ValueError`` when a band entry has an unsupported token, a
        non-integer ``+N`` offset, or cannot be resolved at all. Raises
        ``RepoError`` when a command entry run under ``resolve_commands=True``
        exits non-zero, times out, or produces output that does not parse as
        its declared format.
        """
        workspace_port_base = str(self._config.port_base_for_index(0))
        # Workspace-invariant — same value at every scope. Resolved once here
        # (single source: WorkspaceConfig.service_prefix) and reused below so it
        # is never independently re-derived per branch.
        service_prefix = self._config.service_prefix

        if scope == WORKSPACE_SCOPE:
            base_scope: dict[str, str] = {
                "WINTER_ENV": WORKSPACE_SCOPE,
                "WINTER_ENV_INDEX": "0",
                "WINTER_WORKSPACE_PORT_BASE": workspace_port_base,
                "WINTER_SERVICE_PREFIX": service_prefix,
            }
        else:
            trio = build_env_trio(scope, self._config, self._registry)
            base_scope = {
                **trio,
                "WINTER_WORKSPACE_PORT_BASE": workspace_port_base,
                "WINTER_SERVICE_PREFIX": service_prefix,
            }

        bands = self._config.env_bands
        workspace_band = bands.workspace
        if scope == WORKSPACE_SCOPE:
            feature_band = {}
            named_band = {}
            named_band_label = "env.workspace.vars"
        else:
            feature_band = bands.feature
            # Per-env override band for this scope only. A miss — including any
            # [env.<name>.vars] naming an env that does not exist — yields an
            # empty band and resolves nothing extra.
            named_band = bands.named.get(scope, {})
            named_band_label = f"env.{scope}.vars"

        return self._band_resolver.resolve(
            workspace_band=workspace_band,
            feature_band=feature_band,
            named_band=named_band,
            named_band_label=named_band_label,
            base_scope=base_scope,
            resolve_commands=resolve_commands,
        )
