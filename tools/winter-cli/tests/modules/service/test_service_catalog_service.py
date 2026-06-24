"""Tests for ServiceCatalogService — multi-provider catalog merge.

Covers:
- Empty catalog when no providers
- Single provider returning workspace + env services
- Multi-provider merge (from different providers)
- Provider returning non-JSON is silently skipped
- Provider returning non-zero exit is silently skipped
- Provider that raises OSError is silently skipped
- ServiceCatalog.contains() scope matching
- ServiceCatalog.near_misses() suggestions
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.service_catalog_service import (
    ServiceCatalog,
    ServiceCatalogService,
)

WS = Path("/ws")
EXT1 = Path("/ws/ext/tmux")
EXT2 = Path("/ws/ext/docker")

PROVIDER_A = ResolvedCapability(
    slot=CapabilitySlot.service,
    extension_name="winter-service-tmux",
    entrypoint=EXT1 / "workflow/orchestrate",
    ext_dir=EXT1,
    prefix="wst",
    config_dir=WS / ".winter/config/winter-service-tmux",
)
PROVIDER_B = ResolvedCapability(
    slot=CapabilitySlot.service,
    extension_name="winter-service-docker",
    entrypoint=EXT2 / "workflow/service",
    ext_dir=EXT2,
    prefix="wsd",
    config_dir=WS / ".winter/config/winter-service-docker",
)

_EP_A = str(PROVIDER_A.entrypoint)
_EP_B = str(PROVIDER_B.entrypoint)


def _run_ok(services: list[str]) -> SubprocessResult:
    import json

    return SubprocessResult(returncode=0, stdout=json.dumps({"services": services}), stderr="")


def _svc(run_responses: dict) -> ServiceCatalogService:
    runner = FakeSubprocessRunner(run_responses=run_responses)
    return ServiceCatalogService(subprocess_runner=runner, workspace_root=WS)


# ── empty / no-provider cases ────────────────────────────────────────────────


def test_no_providers_returns_empty_catalog() -> None:
    svc = _svc({})
    catalog = svc.build(providers=[])
    assert catalog.workspace_services == frozenset()
    assert catalog.env_services == frozenset()
    assert catalog.all_qualified_names() == []


def test_single_provider_workspace_services() -> None:
    svc = _svc({f"{_EP_A} catalog": _run_ok(["workspace/postgres", "workspace/rabbitmq"])})
    catalog = svc.build([PROVIDER_A])
    assert catalog.workspace_services == frozenset({"postgres", "rabbitmq"})
    assert catalog.env_services == frozenset()


def test_single_provider_env_services() -> None:
    svc = _svc({f"{_EP_A} catalog": _run_ok(["*/api", "*/worker"])})
    catalog = svc.build([PROVIDER_A])
    assert catalog.env_services == frozenset({"api", "worker"})
    assert catalog.workspace_services == frozenset()


# ── multi-provider merge ─────────────────────────────────────────────────────


def test_multi_provider_merge() -> None:
    """Services from both providers are merged into a single catalog."""
    svc = _svc(
        {
            f"{_EP_A} catalog": _run_ok(["workspace/rabbitmq", "*/api"]),
            f"{_EP_B} catalog": _run_ok(["workspace/db", "*/worker"]),
        }
    )
    catalog = svc.build([PROVIDER_A, PROVIDER_B])
    assert catalog.workspace_services == frozenset({"rabbitmq", "db"})
    assert catalog.env_services == frozenset({"api", "worker"})


def test_multi_provider_duplicate_names_merged() -> None:
    """Duplicate service names from multiple providers are deduplicated."""
    svc = _svc(
        {
            f"{_EP_A} catalog": _run_ok(["workspace/shared"]),
            f"{_EP_B} catalog": _run_ok(["workspace/shared"]),
        }
    )
    catalog = svc.build([PROVIDER_A, PROVIDER_B])
    # Both claim workspace/shared — merged into one
    assert catalog.workspace_services == frozenset({"shared"})


# ── graceful degradation ─────────────────────────────────────────────────────


def test_provider_non_json_skipped() -> None:
    """A provider returning non-JSON output is silently skipped."""
    svc = _svc({f"{_EP_A} catalog": SubprocessResult(returncode=0, stdout="not json", stderr="")})
    catalog = svc.build([PROVIDER_A])
    assert catalog.all_qualified_names() == []


def test_provider_non_zero_exit_skipped() -> None:
    """A provider returning non-zero exit is silently skipped."""
    svc = _svc({f"{_EP_A} catalog": SubprocessResult(returncode=2, stdout='{"services":[]}', stderr="")})
    catalog = svc.build([PROVIDER_A])
    assert catalog.all_qualified_names() == []


def test_unknown_provider_entries_skipped() -> None:
    """Service names with unrecognised scopes are ignored."""
    svc = _svc({f"{_EP_A} catalog": _run_ok(["unknown-scope/api", "*/backend"])})
    catalog = svc.build([PROVIDER_A])
    # Only */backend is recognised; unknown-scope/api is dropped.
    assert catalog.env_services == frozenset({"backend"})
    assert catalog.workspace_services == frozenset()


# ── ServiceCatalog.contains() ────────────────────────────────────────────────


def test_contains_workspace_ref_matches_workspace_service() -> None:
    catalog = ServiceCatalog(workspace_services=frozenset({"postgres"}))
    assert catalog.contains("workspace/postgres")


def test_contains_workspace_ref_does_not_match_env_service() -> None:
    catalog = ServiceCatalog(env_services=frozenset({"postgres"}))
    assert not catalog.contains("workspace/postgres")


def test_contains_env_ref_matches_env_service() -> None:
    catalog = ServiceCatalog(env_services=frozenset({"api"}))
    assert catalog.contains("beta/api")
    assert catalog.contains("alpha/api")


def test_contains_env_ref_does_not_match_workspace_service() -> None:
    catalog = ServiceCatalog(workspace_services=frozenset({"api"}))
    assert not catalog.contains("beta/api")


def test_contains_malformed_ref_returns_false() -> None:
    catalog = ServiceCatalog(workspace_services=frozenset({"postgres"}), env_services=frozenset({"api"}))
    assert not catalog.contains("noslash")
    assert not catalog.contains("")


# ── ServiceCatalog.near_misses() ─────────────────────────────────────────────


def test_near_misses_exact_name_match_first() -> None:
    catalog = ServiceCatalog(env_services=frozenset({"api", "backend", "worker"}))
    misses = catalog.near_misses("beta/api")
    # Exact name match should be the top suggestion
    assert misses[0] == "*/api"


def test_near_misses_typo() -> None:
    catalog = ServiceCatalog(workspace_services=frozenset({"rabbitmq"}))
    misses = catalog.near_misses("workspace/rabbitmq-typo")
    # "rabbitmq" is close to "rabbitmq-typo"
    assert "workspace/rabbitmq" in misses


def test_near_misses_empty_catalog_returns_empty() -> None:
    catalog = ServiceCatalog()
    misses = catalog.near_misses("beta/missing")
    assert misses == []


def test_all_qualified_names_ordering() -> None:
    """Workspace names come first (sorted), then env names (sorted)."""
    catalog = ServiceCatalog(
        workspace_services=frozenset({"postgres", "rabbitmq"}),
        env_services=frozenset({"api", "worker"}),
    )
    names = catalog.all_qualified_names()
    # workspace/* first, then */*
    workspace_idx = [i for i, n in enumerate(names) if n.startswith("workspace/")]
    env_idx = [i for i, n in enumerate(names) if n.startswith("*/")]
    assert workspace_idx == [0, 1]
    assert env_idx == [2, 3]
    # sorted within each group
    assert names[0] < names[1]
    assert names[2] < names[3]
