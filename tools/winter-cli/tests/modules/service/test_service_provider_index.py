from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import FakeSubprocessRunner
from winter_cli.core.subprocess_runner import SubprocessResult
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.describe_parser import DescribeParseError, DescribeResultParser
from winter_cli.modules.service.service_provider_index import (
    DuplicateOwnershipError,
    ServiceDescribeService,
    ServiceProviderIndex,
)

WS = Path("/ws")
EXT_A = WS / "provider-a"
EXT_B = WS / "provider-b"
ENTRYPOINT_A = EXT_A / "workflow/service"
ENTRYPOINT_B = EXT_B / "workflow/service"


# ── helpers ───────────────────────────────────────────────────────────────────


def _provider(name: str, entrypoint: Path, ext_dir: Path) -> ResolvedCapability:
    return ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name=name,
        entrypoint=entrypoint,
        ext_dir=ext_dir,
        prefix=name,
        config_dir=WS / ".winter" / "config" / name,
    )


def _describe_json(*services: str) -> str:
    return json.dumps({"services": list(services)})


def _describe_result(result: str, returncode: int = 0) -> SubprocessResult:
    return SubprocessResult(returncode=returncode, stdout=result, stderr="")


def _svc(runner: FakeSubprocessRunner | None = None) -> ServiceDescribeService:
    return ServiceDescribeService(
        subprocess_runner=runner or FakeSubprocessRunner(),
        describe_parser=DescribeResultParser(),
        workspace_root=WS,
    )


def _describe_key(entrypoint: Path) -> str:
    """Key for FakeSubprocessRunner.run_responses for the describe command."""
    return f"{entrypoint} describe"


# ── ServiceProviderIndex.owner_for — single-provider mode ────────────────────


def test_single_provider_index_owns_any_service() -> None:
    """In single-provider mode, owner_for returns the sole provider for any name."""
    provider = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    idx = ServiceProviderIndex(
        providers_in_order=(provider,),
        sole_provider=provider,
        index={},
    )
    assert idx.owner_for("api") is provider
    assert idx.owner_for("worker") is provider
    assert idx.owner_for("anything-at-all") is provider


def test_single_provider_index_providers_in_order() -> None:
    """providers_in_order returns the single provider tuple."""
    provider = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    idx = ServiceProviderIndex(
        providers_in_order=(provider,),
        sole_provider=provider,
        index={},
    )
    assert idx.providers_in_order == (provider,)


# ── ServiceProviderIndex.owner_for — multi-provider mode ─────────────────────


def test_multi_provider_index_returns_correct_owner() -> None:
    """owner_for returns the correct indexed owner for known service names."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)
    idx = ServiceProviderIndex(
        providers_in_order=(pa, pb),
        sole_provider=None,
        index={"api": pa, "worker": pb},
    )
    assert idx.owner_for("api") is pa
    assert idx.owner_for("worker") is pb


def test_multi_provider_index_unknown_service_returns_none() -> None:
    """owner_for returns None for a service name not in the index."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)
    idx = ServiceProviderIndex(
        providers_in_order=(pa, pb),
        sole_provider=None,
        index={"api": pa},
    )
    assert idx.owner_for("not-registered") is None


def test_multi_provider_index_providers_in_order() -> None:
    """providers_in_order preserves the declared order of providers."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)
    idx = ServiceProviderIndex(
        providers_in_order=(pa, pb),
        sole_provider=None,
        index={},
    )
    assert idx.providers_in_order == (pa, pb)


# ── ServiceDescribeService.build — single-provider short-circuit (D1) ─────────


def test_single_provider_no_describe_call() -> None:
    """With a single provider, build() does NOT call describe — D1 short-circuit."""
    runner = FakeSubprocessRunner()  # no run_responses registered; any run() raises
    provider = _provider("provider-a", ENTRYPOINT_A, EXT_A)

    result = _svc(runner).build([provider])

    # No run() call should have been made.
    assert runner.run_calls == []
    assert result.providers_in_order == (provider,)


def test_single_provider_owner_for_returns_sole() -> None:
    """Index built from a single provider: owner_for always returns that provider."""
    runner = FakeSubprocessRunner()
    provider = _provider("provider-a", ENTRYPOINT_A, EXT_A)

    idx = _svc(runner).build([provider])

    assert idx.owner_for("api") is provider
    assert idx.owner_for("totally-unknown-service") is provider


# ── ServiceDescribeService.build — two providers, correct index ───────────────


def test_two_providers_builds_correct_index() -> None:
    """With two providers, build() calls describe on each and maps services correctly."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("api", "frontend")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("worker", "scheduler")),
        }
    )

    idx = _svc(runner).build([pa, pb])

    assert idx.owner_for("api") is pa
    assert idx.owner_for("frontend") is pa
    assert idx.owner_for("worker") is pb
    assert idx.owner_for("scheduler") is pb
    assert idx.owner_for("unknown") is None


def test_two_providers_describe_called_for_each() -> None:
    """build() invokes describe on both providers."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("api")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("worker")),
        }
    )

    _svc(runner).build([pa, pb])

    # Exactly two run() calls, one per provider.
    assert len(runner.run_calls) == 2
    cmds = [call[0] for call in runner.run_calls]
    assert [str(ENTRYPOINT_A), "describe"] in cmds
    assert [str(ENTRYPOINT_B), "describe"] in cmds


def test_two_providers_describe_sets_env_vars() -> None:
    """describe dispatch injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json()),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json()),
        }
    )

    _svc(runner).build([pa, pb])

    for env in runner.run_envs:
        assert "WINTER_WORKSPACE_DIR" in env
        assert "WINTER_EXT_DIR" in env
        assert "WINTER_EXT_PREFIX" in env


def test_two_providers_providers_in_order_preserved() -> None:
    """providers_in_order from the built index matches the input order."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json()),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json()),
        }
    )

    idx = _svc(runner).build([pa, pb])

    assert idx.providers_in_order == (pa, pb)


def test_two_providers_empty_describe_responses() -> None:
    """Providers returning empty services lists build a valid empty-index."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json()),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json()),
        }
    )

    idx = _svc(runner).build([pa, pb])

    assert idx.owner_for("anything") is None


# ── duplicate-ownership detection (AC4) ──────────────────────────────────────


def test_duplicate_ownership_raises_error() -> None:
    """Two providers claiming the same service name raises DuplicateOwnershipError."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("api", "frontend")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("api", "worker")),
        }
    )

    with pytest.raises(DuplicateOwnershipError):
        _svc(runner).build([pa, pb])


def test_duplicate_ownership_names_service_in_error() -> None:
    """DuplicateOwnershipError message names the conflicting service."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("api")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("api")),
        }
    )

    with pytest.raises(DuplicateOwnershipError, match="api"):
        _svc(runner).build([pa, pb])


def test_duplicate_ownership_names_both_providers() -> None:
    """DuplicateOwnershipError message names both conflicting providers."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("shared-svc")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("shared-svc")),
        }
    )

    with pytest.raises(DuplicateOwnershipError, match="provider-a") as exc_info:
        _svc(runner).build([pa, pb])

    assert "provider-b" in str(exc_info.value)


def test_duplicate_detected_on_second_provider() -> None:
    """The duplicate is detected when processing the second provider, not the first."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("svc1", "conflict")),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("svc2", "conflict")),
        }
    )

    with pytest.raises(DuplicateOwnershipError, match="conflict"):
        _svc(runner).build([pa, pb])


# ── parse error propagation ───────────────────────────────────────────────────


def test_malformed_describe_output_raises_parse_error() -> None:
    """Malformed JSON from a provider propagates as DescribeParseError (no handler)."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result("not valid json {{{"),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("worker")),
        }
    )

    with pytest.raises(DescribeParseError, match="provider-a"):
        _svc(runner).build([pa, pb])


# ── on_describe_error resilience ──────────────────────────────────────────────


def test_on_describe_error_called_for_broken_provider() -> None:
    """on_describe_error is invoked with the broken provider's name instead of raising."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(""),  # empty → parse error
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("worker")),
        }
    )

    errors: list[tuple[str, str]] = []
    idx = _svc(runner).build([pa, pb], on_describe_error=lambda name, detail: errors.append((name, detail)))

    assert len(errors) == 1
    assert errors[0][0] == "provider-a"
    assert errors[0][1]  # non-empty detail


def test_broken_provider_owns_no_services_in_index() -> None:
    """A provider that emits invalid describe contributes no services to the index."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result("not-json"),
            _describe_key(ENTRYPOINT_B): _describe_result(_describe_json("worker", "scheduler")),
        }
    )

    idx = _svc(runner).build([pa, pb], on_describe_error=lambda *_: None)

    assert idx.owner_for("worker") is pb
    assert idx.owner_for("scheduler") is pb
    # provider-a produced no valid services
    assert idx.owner_for("unknown") is None


def test_good_provider_services_indexed_when_other_is_broken() -> None:
    """Services from the conformant provider are accessible in the index."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(_describe_json("api", "frontend")),
            _describe_key(ENTRYPOINT_B): _describe_result(""),  # broken
        }
    )

    idx = _svc(runner).build([pa, pb], on_describe_error=lambda *_: None)

    assert idx.owner_for("api") is pa
    assert idx.owner_for("frontend") is pa
    assert idx.owner_for("worker") is None


def test_both_providers_broken_results_in_empty_index() -> None:
    """When both providers emit invalid describe, the index is empty."""
    pa = _provider("provider-a", ENTRYPOINT_A, EXT_A)
    pb = _provider("provider-b", ENTRYPOINT_B, EXT_B)

    runner = FakeSubprocessRunner(
        run_responses={
            _describe_key(ENTRYPOINT_A): _describe_result(""),
            _describe_key(ENTRYPOINT_B): _describe_result("not-json"),
        }
    )

    errors: list[str] = []
    idx = _svc(runner).build([pa, pb], on_describe_error=lambda name, _: errors.append(name))

    assert len(errors) == 2
    assert idx.owner_for("anything") is None
