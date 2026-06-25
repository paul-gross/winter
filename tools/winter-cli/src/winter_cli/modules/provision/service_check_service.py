"""Real implementation of the IProvisionServiceCheck seam (Phase 5).

Checks ``required_services`` for resource/data handlers before they execute.
Absent ``--no-service-check``, queries each scope-qualified service via the
in-process status seam, starts any owning scope that has a down service, and
leaves it running.  ``--no-service-check`` short-circuits to ``"skipped"``.

Scope-qualified service format: ``"<scope>/<service>"`` where ``<scope>`` is
either the literal string ``"workspace"`` or the target env name.

Cross-env policy: only ``workspace/<svc>`` and ``<env>/<svc>`` tokens (where
``<env>`` matches the provision target) are supported.  Tokens referencing a
different env are rejected with a clear error at ensure()-time.
"""

from __future__ import annotations

import click

from winter_cli.modules.provision.manifest import ProvisionHandler
from winter_cli.modules.service.scope import WORKSPACE_SCOPE
from winter_cli.modules.service.service_dispatch_service import ServiceDispatchService
from winter_cli.modules.service.service_status_service import ServiceStatusService
from winter_cli.modules.service.status_models import StatusDocument
from winter_cli.modules.workspace.models import RepoError

# The state value reported by the orchestrator when a service is running.
_RUNNING_STATE = "running"


class ProvisionServiceCheck:
    """Implements IProvisionServiceCheck using the in-process service seams.

    Injected with the status service (for non-rendering status queries) and
    the dispatch service (for ``up`` calls).  The orchestrator resolver is
    kept as a thin way to detect whether any orchestrator is registered — the
    real dispatch and status calls auto-resolve via their own injected resolver.
    """

    def __init__(
        self,
        status_svc: ServiceStatusService,
        dispatch_svc: ServiceDispatchService,
    ) -> None:
        self._status_svc = status_svc
        self._dispatch_svc = dispatch_svc

    def ensure(
        self,
        handlers: list[ProvisionHandler],
        env_name: str,
        no_service_check: bool,
    ) -> str | None:
        """Check and (if needed) start required services.

        Returns:
        - ``None`` when no handler declares ``required_services``.
        - ``"skipped"`` when ``no_service_check`` is True.
        - ``"ok"`` when all required services are already running.
        - ``"started:<scopes>"`` when one or more owning scopes were started,
          where ``<scopes>`` is a comma-separated list of scope names started.

        Raises ``click.ClickException`` when:
        - ``required_services`` is non-empty but no orchestrator is registered.
        - A service token references an env other than ``env_name`` or ``workspace``.
        """
        # Step 1: collect union of required_services across all handlers.
        union: set[str] = set()
        for h in handlers:
            union.update(h.required_services)

        # No required_services on any handler — skip silently, return None.
        if not union:
            return None

        # Step 2: --no-service-check — skip entirely, no orchestrator needed.
        if no_service_check:
            return "skipped"

        # Step 3: validate token format and check for cross-env references.
        _validate_tokens(union, env_name)

        # Step 4: probe the orchestrator; raise a clear error if none registered.
        # We call collect() which will internally call resolve_all() on the registry.
        # If no orchestrator is registered, resolve_all() raises RepoError (or its
        # subclass CapabilityBindingError).  We intercept that here and convert it to
        # a user-facing ClickException specific to the provision context.
        sorted_tokens = sorted(union)
        try:
            doc = self._status_svc.collect(tuple(sorted_tokens))
        except RepoError as exc:
            raise click.ClickException(
                f"winter provision: required_services is declared but no service orchestrator is registered.\n"
                f"  Required services: {', '.join(sorted_tokens)}\n"
                f"  Register a service orchestrator in .winter/config.toml to use required_services.\n"
                f"  (Underlying error: {exc})"
            ) from exc

        # Step 5: classify each token as up/down.
        down_tokens = _classify_down(sorted_tokens, doc)

        if not down_tokens:
            return "ok"

        # Step 6: group down tokens by owning scope; dispatch up once per scope.
        scopes_to_start = _owning_scopes(down_tokens, env_name)

        started: list[str] = []
        for scope in sorted(scopes_to_start):
            exit_code = self._dispatch_svc.dispatch("up", [scope])
            if exit_code != 0:
                raise click.ClickException(
                    f"winter provision: failed to start owning scope {scope!r} for required services "
                    f"(exit code {exit_code})."
                )
            started.append(scope)

        if started:
            return f"started:{','.join(started)}"
        return "ok"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_tokens(tokens: set[str], env_name: str) -> None:
    """Raise ClickException for any token that is not well-formed or references a foreign env."""
    for token in sorted(tokens):
        parts = token.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise click.ClickException(
                f"winter provision: malformed required_services token {token!r}. "
                f"Expected format: 'workspace/<service>' or '{env_name}/<service>'."
            )
        scope, _svc = parts
        if scope != WORKSPACE_SCOPE and scope != env_name:
            raise click.ClickException(
                f"winter provision: required_services token {token!r} references env {scope!r} "
                f"but this provision run targets env {env_name!r}. "
                f"Only 'workspace/<service>' and '{env_name}/<service>' tokens are allowed."
            )


def _classify_down(tokens: list[str], doc: StatusDocument | None) -> list[str]:
    """Return the subset of *tokens* that are NOT running.

    A token is considered "up" when the status document contains a service
    entry under the matching env with state == "running".  Running-state is
    the key — health is observability-only and is ignored.

    A token is "down" when:
    - The document is None (no provider produced parseable output).
    - The env is absent from the document.
    - The service is absent from the env's service list.
    - The service is present but state != "running".
    """
    if doc is None:
        return list(tokens)

    # Build a lookup: (env_name, service_name) -> state
    running: set[tuple[str, str]] = set()
    for env_status in doc.envs:
        for svc in env_status.services:
            if svc.state == _RUNNING_STATE:
                running.add((env_status.env, svc.name))

    down: list[str] = []
    for token in tokens:
        scope, svc_name = token.split("/", 1)
        if (scope, svc_name) not in running:
            down.append(token)
    return down


def _owning_scopes(down_tokens: list[str], env_name: str) -> set[str]:
    """Return the set of owning scopes to bring up for the given down tokens.

    A ``workspace/<svc>`` token's owning scope is ``"workspace"``.
    An ``<env>/<svc>`` token's owning scope is the env name.
    """
    scopes: set[str] = set()
    for token in down_tokens:
        scope, _svc = token.split("/", 1)
        if scope == WORKSPACE_SCOPE:
            scopes.add(WORKSPACE_SCOPE)
        else:
            scopes.add(env_name)
    return scopes
