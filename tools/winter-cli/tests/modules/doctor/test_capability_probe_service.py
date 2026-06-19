from __future__ import annotations

from pathlib import Path

from winter_cli.modules.capability.models import (
    CapabilityCandidate,
    CapabilitySlot,
    SlotResolution,
)
from winter_cli.modules.doctor.capability_probe_service import (
    CAPABILITY_SOURCE,
    CapabilityProbeService,
)
from winter_cli.modules.doctor.models import ProbeStatus


def _candidate(
    extension_name: str,
    entrypoint_valid: bool = True,
    entrypoint_rel: str = "workflow/service",
) -> CapabilityCandidate:
    return CapabilityCandidate(
        extension_name=extension_name,
        entrypoint_rel=entrypoint_rel,
        entrypoint_path=Path(f"/ext/{extension_name}/{entrypoint_rel}"),
        ext_dir=Path(f"/ext/{extension_name}"),
        prefix="wf",
        entrypoint_valid=entrypoint_valid,
    )


def _resolution(
    *,
    binding_kind: str,
    candidates: tuple[CapabilityCandidate, ...] = (),
    bound_extension: str | None = None,
    error: str | None = None,
    slot: CapabilitySlot = CapabilitySlot.service,
) -> SlotResolution:
    return SlotResolution(
        slot=slot,
        candidates=candidates,
        bound_extension=bound_extension,
        binding_kind=binding_kind,  # type: ignore[arg-type]
        error=error,
    )


class _StubRegistry:
    def __init__(self, resolutions: list[SlotResolution]) -> None:
        self._resolutions = resolutions

    def describe_all(self) -> list[SlotResolution]:
        return list(self._resolutions)


def _svc(resolutions: list[SlotResolution]) -> CapabilityProbeService:
    return CapabilityProbeService(registry=_StubRegistry(resolutions))  # type: ignore[arg-type]


# ── explicit-valid → pass ─────────────────────────────────────────────────────


def test_explicit_binding_emits_pass() -> None:
    res = _resolution(
        binding_kind="explicit",
        candidates=(_candidate("winter-service-tmux"),),
        bound_extension="winter-service-tmux",
    )
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.source == CAPABILITY_SOURCE
    assert r.name == "slot: service"
    assert r.status == ProbeStatus.pass_
    assert "winter-service-tmux" in r.message


# ── implicit (sole provider, valid entrypoint) → pass with note ───────────────


def test_implicit_binding_emits_pass_with_note() -> None:
    res = _resolution(
        binding_kind="implicit",
        candidates=(_candidate("winter-service-tmux"),),
    )
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.pass_
    assert "implicitly bound" in r.message
    assert "winter-service-tmux" in r.message
    assert "sole provider" in r.message


# ── implicit with bad entrypoint → fail ──────────────────────────────────────


def test_implicit_binding_bad_entrypoint_emits_fail() -> None:
    res = _resolution(
        binding_kind="implicit",
        candidates=(_candidate("winter-service-tmux", entrypoint_valid=False),),
    )
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "winter-service-tmux" in r.message
    assert "entrypoint not found" in r.message
    assert r.remediation is not None


# ── ambiguous (unbound, ≥2 candidates) → fail naming all candidates ──────────


def test_ambiguous_emits_fail_naming_all_candidates() -> None:
    res = _resolution(
        binding_kind="unbound",
        candidates=(_candidate("ext-a"), _candidate("ext-b")),
    )
    assert res.is_ambiguous
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert "ext-a" in r.message
    assert "ext-b" in r.message
    assert r.remediation is not None
    assert "capabilities.service" in r.remediation


# ── invalid binding → fail with error message ────────────────────────────────


def test_invalid_binding_emits_fail_with_error_message() -> None:
    error_msg = "capabilities.service = 'missing-ext' — no installed extension named 'missing-ext'"
    res = _resolution(
        binding_kind="invalid",
        bound_extension="missing-ext",
        error=error_msg,
    )
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert r.message == error_msg
    assert r.remediation is not None


# ── no provider installed (unbound, 0 candidates) → warn ─────────────────────


def test_no_provider_emits_warn() -> None:
    res = _resolution(binding_kind="unbound", candidates=())
    assert not res.is_ambiguous
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.warn
    assert "no provider installed" in r.message


# ── one result per slot ───────────────────────────────────────────────────────


def test_one_result_per_slot_returned() -> None:
    resolutions = [
        _resolution(binding_kind="explicit", bound_extension="ext-a", candidates=(_candidate("ext-a"),)),
    ]
    svc = _svc(resolutions)

    results = svc.run()

    assert len(results) == 1


# ── probe names use slot value ────────────────────────────────────────────────


def test_probe_name_uses_slot_value() -> None:
    res = _resolution(binding_kind="unbound", candidates=())
    svc = _svc([res])

    results = svc.run()

    assert results[0].name == f"slot: {CapabilitySlot.service.value}"


# ── incompatible binding → fail with both versions in the message ────────────


def test_incompatible_binding_emits_fail_with_version_message() -> None:
    error_msg = (
        "capabilities.service provider implements service spec v2, but this winter supports {v1}."
        " Upgrade winter or pin the extension to a supported version."
    )
    res = _resolution(
        binding_kind="incompatible",
        bound_extension="winter-service-tmux",
        candidates=(_candidate("winter-service-tmux"),),
        error=error_msg,
    )
    svc = _svc([res])

    results = svc.run()

    assert len(results) == 1
    r = results[0]
    assert r.status == ProbeStatus.fail
    assert r.message == error_msg
    assert r.remediation is not None
    assert "Upgrade winter" in r.remediation or "supported" in r.remediation
