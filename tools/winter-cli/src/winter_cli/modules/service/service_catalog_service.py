"""Service catalog service — queries providers for their declared service names.

Invokes each bound service-orchestrator provider with the ``catalog`` action and
merges the results into a single set of scope-qualified service names.

Catalog output format per provider::

    {"services": ["workspace/<name>", "*/<name>", ...]}

  - ``workspace/<name>`` — a workspace-scoped service (matches only
    ``workspace/<name>`` references in provision manifests).
  - ``*/<name>``         — an env-scoped service (matches ``<any-env>/<name>``
    references in provision manifests, where the env segment is not ``workspace``).

The merged catalog exposes the same format so callers never need to know which
provider owns a service.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.capability.models import ResolvedCapability
from winter_cli.modules.service.provider_invocation import build_provider_env

logger = logging.getLogger(__name__)

# The scope prefix that identifies a workspace-scoped service in the catalog.
WORKSPACE_SCOPE = "workspace"

# The wildcard prefix used by providers for env-scoped services (any env name
# matches this prefix in the provision manifest lint check).
ENV_WILDCARD = "*"


@dataclass(frozen=True)
class ServiceCatalog:
    """Merged service catalog from one or more providers.

    ``workspace_services`` — service names declared with workspace scope.
    ``env_services``       — service names declared with env scope (env-agnostic).
    """

    workspace_services: frozenset[str] = field(default_factory=frozenset)
    env_services: frozenset[str] = field(default_factory=frozenset)

    def all_qualified_names(self) -> list[str]:
        """Return all scope-qualified names in deterministic order.

        Format: ``workspace/<name>`` first (sorted), then ``*/<name>`` (sorted).
        """
        ws = sorted(f"{WORKSPACE_SCOPE}/{n}" for n in self.workspace_services)
        env = sorted(f"{ENV_WILDCARD}/{n}" for n in self.env_services)
        return ws + env

    def contains(self, qualified_ref: str) -> bool:
        """Return True when ``qualified_ref`` is in the catalog.

        ``workspace/<name>`` matches only workspace services.
        ``<env>/<name>`` (where env != "workspace") matches env services.
        """
        parts = qualified_ref.split("/", 1)
        if len(parts) != 2:
            return False
        scope, name = parts
        if scope == WORKSPACE_SCOPE:
            return name in self.workspace_services
        return name in self.env_services

    def near_misses(self, qualified_ref: str, max_results: int = 3) -> list[str]:
        """Return up to ``max_results`` scope-qualified names close to ``qualified_ref``.

        Uses a simple character-overlap similarity (intersection of character bigrams)
        to rank candidates.  A substring match or exact-name match always scores
        highest.
        """
        parts = qualified_ref.split("/", 1)
        svc_name = parts[1] if len(parts) == 2 else qualified_ref

        candidates = self.all_qualified_names()
        if not candidates:
            return []

        def score(candidate: str) -> float:
            cand_parts = candidate.split("/", 1)
            cand_name = cand_parts[1] if len(cand_parts) == 2 else candidate
            if cand_name == svc_name:
                return 1.0
            if svc_name in cand_name or cand_name in svc_name:
                return 0.8
            # bigram overlap
            a_bigrams = _bigrams(svc_name)
            b_bigrams = _bigrams(cand_name)
            if not a_bigrams and not b_bigrams:
                return 0.0
            overlap = len(a_bigrams & b_bigrams)
            union = len(a_bigrams | b_bigrams)
            return overlap / union if union else 0.0

        scored = sorted(candidates, key=score, reverse=True)
        threshold = 0.1
        return [c for c in scored[:max_results] if score(c) >= threshold]


def _bigrams(s: str) -> frozenset[str]:
    if len(s) < 2:
        return frozenset()
    return frozenset(s[i : i + 2] for i in range(len(s) - 1))


class ServiceCatalogService:
    """Queries every bound service-orchestrator provider for its catalog.

    Each provider is invoked as ``<entrypoint> catalog`` (no extra args).
    The JSON response ``{"services": [...]}`` is parsed; names are split
    into workspace vs env partitions by their ``workspace/`` prefix.

    If a provider does not respond, returns empty output, or emits malformed
    JSON, its result is silently omitted (graceful degradation: providers that
    predate the ``catalog`` action simply return nothing).

    The service supports an explicit no-provider case (``has_providers=False``)
    so the lint check can distinguish "no orchestrator registered" from "empty
    catalog".
    """

    def __init__(
        self,
        subprocess_runner: ISubprocessRunner,
        workspace_root: Path,
    ) -> None:
        self._runner = subprocess_runner
        self._workspace_root = workspace_root

    def build(self, providers: list[ResolvedCapability]) -> ServiceCatalog:
        """Invoke each provider and return the merged catalog."""
        workspace_names: set[str] = set()
        env_names: set[str] = set()

        for provider in providers:
            names = self._query_provider(provider)
            for qualified in names:
                parts = qualified.split("/", 1)
                if len(parts) != 2:
                    continue
                scope, name = parts
                if scope == WORKSPACE_SCOPE:
                    workspace_names.add(name)
                elif scope == ENV_WILDCARD:
                    env_names.add(name)
                # Ignore other forms (forward-compat).

        return ServiceCatalog(
            workspace_services=frozenset(workspace_names),
            env_services=frozenset(env_names),
        )

    def _query_provider(self, provider: ResolvedCapability) -> list[str]:
        """Invoke ``<entrypoint> catalog`` and return the service name list.

        Returns an empty list on any failure (missing file, non-zero exit,
        malformed JSON) so unknown providers are silently skipped.
        """
        cmd = [str(provider.entrypoint), "catalog"]
        env = build_provider_env(provider, self._workspace_root)
        try:
            result = self._runner.run(cmd, cwd=self._workspace_root, env=env)
        except OSError as exc:
            logger.debug("catalog: could not invoke %s: %s", provider.extension_name, exc)
            return []

        if result.returncode != 0:
            logger.debug("catalog: provider %s returned exit %d", provider.extension_name, result.returncode)
            return []

        try:
            obj = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.debug("catalog: provider %s returned non-JSON", provider.extension_name)
            return []

        if not isinstance(obj, dict):
            return []

        raw = obj.get("services", [])
        if not isinstance(raw, list):
            return []

        return [s for s in raw if isinstance(s, str)]
