"""Service-to-provider ownership index for multi-provider service routing.

``ServiceProviderIndex`` is the runtime index that answers "which provider owns
service X?".  ``ServiceDescribeService`` builds it by calling ``describe`` on each
provider in the ordered list.

Design notes:
  - Single-provider short-circuit (D1): when exactly one provider is bound,
    ``describe`` is never called — the sole provider owns every service name.
    ``ServiceProviderIndex.owner_for`` returns the sole provider without a lookup.
  - Duplicate-ownership detection (AC4): if two providers claim the same service
    name, ``ServiceDescribeService.build`` raises ``DuplicateOwnershipError`` with
    an actionable message naming the service and both providers.
  - ``describe`` is captured via ``ISubprocessRunner.run`` (stdout-capturing path).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.describe_parser import DescribeParseError, DescribeResultParser
from winter_cli.modules.service.provider_invocation import build_provider_env


class DuplicateOwnershipError(Exception):
    """Raised when two providers claim ownership of the same service name.

    The message is human-readable and actionable — it names the duplicate service
    and both provider names so the operator can resolve the conflict.
    """


class ServiceProviderIndex:
    """Answers "which provider owns service X?" for the registered ordered providers.

    In single-provider mode (``sole_provider`` is not None) every service name
    resolves to that provider — no ``describe`` call is needed and no index is built.

    In multi-provider mode the index is built from the ``describe`` output of each
    provider in order, and ``owner_for`` looks up the owning provider for a given
    service name (returns ``None`` for unknown names).

    ``providers_in_order`` exposes the full ordered list of resolved providers.
    """

    def __init__(
        self,
        providers_in_order: tuple[ResolvedCapability, ...],
        sole_provider: ResolvedCapability | None,
        index: dict[str, ResolvedCapability],
    ) -> None:
        self._providers_in_order = providers_in_order
        self._sole_provider = sole_provider
        self._index = index

    @property
    def providers_in_order(self) -> tuple[ResolvedCapability, ...]:
        """The ordered list of all resolved providers."""
        return self._providers_in_order

    @property
    def is_sole_provider(self) -> bool:
        """True when in single-provider mode (sole provider owns every service)."""
        return self._sole_provider is not None

    def known_service_names(self) -> tuple[str, ...]:
        """Return the service names explicitly claimed by providers via ``describe``.

        In single-provider mode this is always empty (the sole provider owns every
        service implicitly — no ``describe`` call was made). In multi-provider mode
        this is the union of all service names returned by each provider's ``describe``.
        """
        return tuple(self._index.keys())

    def names_owned_by(self, provider: ResolvedCapability) -> frozenset[str]:
        """Return the set of service names explicitly owned by ``provider``.

        In single-provider mode the index is empty and the sole provider owns every
        service implicitly — returns an empty frozenset (callers check ``is_sole_provider``
        to distinguish "owns all implicitly" from "owns nothing explicitly").

        In multi-provider mode returns the subset of indexed names where ``provider``
        is the recorded owner.
        """
        return frozenset(name for name, p in self._index.items() if p == provider)

    def owner_for(self, service_name: str) -> ResolvedCapability | None:
        """Return the owning provider for ``service_name``, or ``None`` if unknown.

        In single-provider mode the sole provider is always returned regardless of
        the service name.  In multi-provider mode the indexed owner is returned, or
        ``None`` if no provider claimed the service.
        """
        if self._sole_provider is not None:
            return self._sole_provider
        return self._index.get(service_name)


class ServiceDescribeService:
    """Builds the service-to-provider ownership index from the ordered providers.

    For a single provider the index is trivially the sole provider (D1 short-circuit
    — no ``describe`` subprocess call is made).

    For two or more providers, ``describe`` is invoked on each provider in order via
    ``ISubprocessRunner.run`` (stdout-capturing path, matching the status-capture
    pattern).  Each provider's stdout JSON is parsed into a ``DescribeResult``.
    Duplicate ownership (same service name claimed by two providers) raises
    ``DuplicateOwnershipError`` at index-build time.
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        describe_parser: DescribeResultParser,
        workspace_root: Path,
    ) -> None:
        self._subprocess_runner = subprocess_runner
        self._describe_parser = describe_parser
        self._workspace_root = workspace_root

    def build(
        self,
        providers: list[ResolvedCapability],
        *,
        on_describe_error: Callable[[str, str], None] | None = None,
    ) -> ServiceProviderIndex:
        """Build and return the ownership index for ``providers``.

        When ``on_describe_error`` is supplied, a provider that emits empty or
        non-JSON describe output is skipped with a single call to
        ``on_describe_error(provider_name, error_detail)`` rather than raising.
        The broken provider is omitted from the index — it owns no services.

        Raises ``DuplicateOwnershipError`` when two providers claim the same service.
        Raises ``DescribeParseError`` when a provider's stdout cannot be parsed and
        no ``on_describe_error`` handler is provided.
        """
        providers_tuple = tuple(providers)

        if len(providers) == 1:
            # D1 short-circuit: sole provider owns everything; no describe call.
            return ServiceProviderIndex(
                providers_in_order=providers_tuple,
                sole_provider=providers[0],
                index={},
            )

        index: dict[str, ResolvedCapability] = {}
        for provider in providers:
            cmd = [str(provider.entrypoint), "describe"]
            merged = build_provider_env(provider, self._workspace_root)

            result = self._subprocess_runner.run(cmd, cwd=self._workspace_root, env=merged)
            try:
                describe_result = self._describe_parser.parse(result.stdout, provider_name=provider.extension_name)
            except DescribeParseError as exc:
                if on_describe_error is not None:
                    on_describe_error(provider.extension_name, str(exc))
                    continue
                raise

            for service_name in describe_result.services:
                if service_name in index:
                    existing = index[service_name]
                    raise DuplicateOwnershipError(
                        f"service {service_name!r} is claimed by both"
                        f" {existing.extension_name!r} and {provider.extension_name!r};"
                        f" each service must be owned by exactly one provider."
                    )
                index[service_name] = provider

        return ServiceProviderIndex(
            providers_in_order=providers_tuple,
            sole_provider=None,
            index=index,
        )
