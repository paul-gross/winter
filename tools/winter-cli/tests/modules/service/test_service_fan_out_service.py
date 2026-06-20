"""Tests for ServiceFanOutService — simple up/down fan-out (no readiness gate).

Coverage:
- Single-provider up: calls provider's up, returns 0.
- Single-provider up: propagates non-zero exit code.
- Single-provider down: calls provider's down, returns 0.
- Single-provider down: propagates non-zero exit code.
- Two-provider up: calls both providers' up in forward order with NO status poll
  between them (proves the gate is gone).
- Two-provider up: aborts on first provider failure; second provider's up never called.
- Two-provider down: calls both providers' down (best-effort).
- Two-provider down: continues past failure; returns first non-zero.
- Two-provider down: returns 0 when all succeed.
- Env vars are injected correctly for each provider.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeSubprocessRunner
from winter_cli.modules.capability.models import CapabilitySlot, ResolvedCapability
from winter_cli.modules.service.service_fan_out_service import ServiceFanOutService

WS = Path("/ws")
EXT_A = WS / "provider-a"
EXT_B = WS / "provider-b"
ENTRYPOINT_A = EXT_A / "workflow/service"
ENTRYPOINT_B = EXT_B / "workflow/service"

_EP_A = str(ENTRYPOINT_A)
_EP_B = str(ENTRYPOINT_B)

_UP_A = f"{_EP_A} up alpha"
_UP_B = f"{_EP_B} up alpha"
_DOWN_A = f"{_EP_A} down alpha"
_DOWN_B = f"{_EP_B} down alpha"


# ── helpers ───────────────────────────────────────────────────────────────────


def _provider(name: str, entrypoint: Path, ext_dir: Path) -> ResolvedCapability:
    return ResolvedCapability(
        slot=CapabilitySlot.service,
        extension_name=name,
        entrypoint=entrypoint,
        ext_dir=ext_dir,
        prefix=name,
    )


def _pa() -> ResolvedCapability:
    return _provider("provider-a", ENTRYPOINT_A, EXT_A)


def _pb() -> ResolvedCapability:
    return _provider("provider-b", ENTRYPOINT_B, EXT_B)


def _make_fan_out(runner: FakeSubprocessRunner) -> ServiceFanOutService:
    return ServiceFanOutService(
        subprocess_runner=runner,
        workspace_root=WS,
    )


# ── single-provider up ────────────────────────────────────────────────────────


def test_single_provider_up_calls_up_returns_zero() -> None:
    """Single-provider up: calls the provider's up and returns 0."""
    pa = _pa()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.up("alpha", [pa])

    assert code == 0
    assert runner.call_calls == [([_EP_A, "up", "alpha"], WS)]
    # No status poll (no gate).
    assert runner.run_calls == []


def test_single_provider_up_propagates_nonzero_exit() -> None:
    """Single-provider up: propagates non-zero exit code."""
    pa = _pa()
    runner = FakeSubprocessRunner(call_responses={_UP_A: 5})
    svc = _make_fan_out(runner)

    code = svc.up("alpha", [pa])

    assert code == 5


# ── single-provider down ──────────────────────────────────────────────────────


def test_single_provider_down_calls_down_returns_zero() -> None:
    """Single-provider down: calls the provider's down and returns 0."""
    pa = _pa()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down("alpha", [pa])

    assert code == 0
    assert runner.call_calls == [([_EP_A, "down", "alpha"], WS)]
    assert runner.run_calls == []


def test_single_provider_down_propagates_nonzero_exit() -> None:
    """Single-provider down: propagates non-zero exit code."""
    pa = _pa()
    runner = FakeSubprocessRunner(call_responses={_DOWN_A: 3})
    svc = _make_fan_out(runner)

    code = svc.down("alpha", [pa])

    assert code == 3


# ── two-provider up (no gate) ─────────────────────────────────────────────────


def test_two_provider_up_calls_both_in_forward_order_no_status_poll() -> None:
    """Two-provider up: calls both providers' up in forward order with NO status poll between.

    This is the primary proof that the readiness gate is gone: no status call
    sits between the two up calls.
    """
    pa = _pa()
    pb = _pb()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.up("alpha", [pa, pb])

    assert code == 0

    # Exact call sequence: up-a then up-b, nothing else.
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert call_cmds == [
        (_EP_A, "up", "alpha"),
        (_EP_B, "up", "alpha"),
    ]

    # No run() calls at all — proves no status poll between the two ups.
    assert runner.run_calls == []


def test_two_provider_up_aborts_on_first_failure_second_never_called() -> None:
    """Two-provider up: if the first provider's up exits non-zero, the second is never called."""
    pa = _pa()
    pb = _pb()
    runner = FakeSubprocessRunner(call_responses={_UP_A: 7})
    svc = _make_fan_out(runner)

    code = svc.up("alpha", [pa, pb])

    assert code == 7
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    # Only provider-a's up was called.
    assert (_EP_A, "up", "alpha") in call_cmds
    assert (_EP_B, "up", "alpha") not in call_cmds
    # No status poll.
    assert runner.run_calls == []


# ── two-provider down (best-effort) ──────────────────────────────────────────


def test_two_provider_down_calls_both_providers() -> None:
    """Two-provider down: calls both providers' down."""
    pa = _pa()
    pb = _pb()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down("alpha", [pa, pb])

    assert code == 0
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_B, "down", "alpha") in call_cmds


def test_two_provider_down_best_effort_continues_on_failure() -> None:
    """Down is best-effort: continues past provider failure; returns first non-zero."""
    pa = _pa()
    pb = _pb()
    # Provider A fails.
    runner = FakeSubprocessRunner(call_responses={_DOWN_A: 4})
    svc = _make_fan_out(runner)

    code = svc.down("alpha", [pa, pb])

    # Both down calls were made (best-effort continues).
    call_cmds = [tuple(c[0]) for c in runner.call_calls]
    assert (_EP_A, "down", "alpha") in call_cmds
    assert (_EP_B, "down", "alpha") in call_cmds
    # First non-zero returned.
    assert code == 4


def test_two_provider_down_all_succeed_returns_zero() -> None:
    """Down returns 0 when all providers succeed."""
    pa = _pa()
    pb = _pb()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    code = svc.down("alpha", [pa, pb])

    assert code == 0


# ── env var injection ─────────────────────────────────────────────────────────


def test_up_injects_provider_env_vars() -> None:
    """Fan-out up injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX per provider."""
    pa = _pa()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.up("alpha", [pa])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert call_env["WINTER_EXT_DIR"] == str(EXT_A)
    assert call_env["WINTER_EXT_PREFIX"] == "provider-a"


def test_down_injects_provider_env_vars() -> None:
    """Fan-out down injects WINTER_WORKSPACE_DIR, WINTER_EXT_DIR, WINTER_EXT_PREFIX per provider."""
    pb = _pb()
    runner = FakeSubprocessRunner()
    svc = _make_fan_out(runner)

    svc.down("alpha", [pb])

    assert len(runner.call_envs) == 1
    call_env = runner.call_envs[0]
    assert call_env["WINTER_WORKSPACE_DIR"] == str(WS)
    assert call_env["WINTER_EXT_DIR"] == str(EXT_B)
    assert call_env["WINTER_EXT_PREFIX"] == "provider-b"
