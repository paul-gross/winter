from __future__ import annotations

import json
from pathlib import Path

from winter_cli.modules.capability.capability_reporter import JsonCapabilityReporter, StreamCapabilityReporter
from winter_cli.modules.capability.models import CapabilityCandidate, CapabilitySlot, SlotResolution


class FakeClick:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def echo(self, message: str = "", err: bool = False) -> None:
        self.lines.append(message)

    def style(self, text: str, **kwargs: object) -> str:
        return text


_WS = Path("/ws")
_TMUX = _WS / "winter-service-tmux"
_DOCKER = _WS / "winter-service-docker"


def _candidate(
    name: str,
    ext_dir: Path,
    entrypoint_rel: str = "workflow/service",
    valid: bool = True,
) -> CapabilityCandidate:
    return CapabilityCandidate(
        extension_name=name,
        entrypoint_rel=entrypoint_rel,
        entrypoint_path=ext_dir / entrypoint_rel,
        ext_dir=ext_dir,
        prefix=name,
        entrypoint_valid=valid,
    )


def _tmux_candidate(valid: bool = True) -> CapabilityCandidate:
    return _candidate("winter-service-tmux", _TMUX, valid=valid)


def _docker_candidate(valid: bool = True) -> CapabilityCandidate:
    return _candidate("winter-service-docker", _DOCKER, valid=valid)


# ── Stream reporter ───────────────────────────────────────────────────────────


def test_stream_explicit_valid() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(valid=True),),
        bound_extension="winter-service-tmux",
        binding_kind="explicit",
        error=None,
    )
    StreamCapabilityReporter(click).render([resolution])
    assert len(click.lines) == 1
    line = click.lines[0]
    assert "service" in line
    assert "winter-service-tmux" in line
    assert "explicit" in line
    assert "✓" in line


def test_stream_implicit_valid() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(valid=True),),
        bound_extension=None,
        binding_kind="implicit",
        error=None,
    )
    StreamCapabilityReporter(click).render([resolution])
    assert len(click.lines) == 1
    line = click.lines[0]
    assert "service" in line
    assert "winter-service-tmux" in line
    assert "implicit" in line
    assert "✓" in line


def test_stream_no_provider() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(),
        bound_extension=None,
        binding_kind="unbound",
        error=None,
    )
    StreamCapabilityReporter(click).render([resolution])
    assert len(click.lines) == 1
    assert "no provider" in click.lines[0]


def test_stream_invalid() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(),
        bound_extension="winter-service-tmux",
        binding_kind="invalid",
        error="capabilities.service provider 'winter-service-tmux' entrypoint not found at /ws/winter-service-tmux/workflow/service.",
    )
    StreamCapabilityReporter(click).render([resolution])
    assert len(click.lines) == 1
    line = click.lines[0]
    assert "invalid" in line
    assert "winter-service-tmux" in line
    assert "entrypoint not found" in line


def test_stream_explicit_invalid_entrypoint() -> None:
    """Explicit binding where candidate has invalid entrypoint shows ✗."""
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(valid=False),),
        bound_extension="winter-service-tmux",
        binding_kind="invalid",
        error="capabilities.service provider 'winter-service-tmux' entrypoint not found at /ws/winter-service-tmux/workflow/service.",
    )
    StreamCapabilityReporter(click).render([resolution])
    line = click.lines[0]
    assert "invalid" in line


# ── JSON reporter ─────────────────────────────────────────────────────────────


def test_json_explicit_valid() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(valid=True),),
        bound_extension="winter-service-tmux",
        binding_kind="explicit",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    assert len(payload) == 1
    obj = payload[0]
    assert obj["slot"] == "service"
    assert obj["bound"] == "winter-service-tmux"
    assert obj["binding_kind"] == "explicit"
    assert obj["ambiguous"] is False
    assert obj["error"] is None
    assert obj["candidates"] == [{"extension": "winter-service-tmux", "entrypoint": "workflow/service", "valid": True}]


def test_json_implicit() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(valid=True),),
        bound_extension=None,
        binding_kind="implicit",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    assert obj["bound"] is None
    assert obj["binding_kind"] == "implicit"
    assert obj["ambiguous"] is False


def test_json_no_provider() -> None:
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(),
        bound_extension=None,
        binding_kind="unbound",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    assert obj["ambiguous"] is False
    assert obj["candidates"] == []
    assert obj["bound"] is None


def test_json_invalid() -> None:
    click = FakeClick()
    err_msg = "capabilities.service = 'missing' — no installed extension named 'missing'"
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(),
        bound_extension="missing",
        binding_kind="invalid",
        error=err_msg,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    assert obj["binding_kind"] == "invalid"
    assert obj["error"] == err_msg
    assert obj["bound"] == "missing"


def test_json_emits_single_line() -> None:
    """JSON reporter emits exactly one echo call."""
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(),),
        bound_extension="winter-service-tmux",
        binding_kind="explicit",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    assert len(click.lines) == 1


# ── Multi-provider (service_orchestrators ordered list) ────────────────────────


def _multi_resolution(
    bound_extensions: tuple[str, ...],
    binding_kind: str = "explicit",
    error: str | None = None,
) -> SlotResolution:
    """Build a SlotResolution with multiple bound providers."""
    return SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(), _docker_candidate()),
        bound_extension=bound_extensions[0] if bound_extensions else None,
        bound_extensions=bound_extensions,
        binding_kind=binding_kind,  # type: ignore[arg-type]
        error=error,
    )


def test_stream_multi_provider_explicit_shows_ordered_list() -> None:
    """Stream reporter shows all providers for a multi-provider slot with (explicit) label."""
    click = FakeClick()
    resolution = _multi_resolution(
        bound_extensions=("winter-service-tmux", "winter-service-docker"),
    )
    StreamCapabilityReporter(click).render([resolution])
    # First line names both providers
    assert "winter-service-tmux" in click.lines[0]
    assert "winter-service-docker" in click.lines[0]
    assert "explicit" in click.lines[0]
    assert "ordered" not in click.lines[0]
    # One indented line per provider
    assert len(click.lines) == 3
    assert click.lines[1].startswith("  - ")
    assert "winter-service-tmux" in click.lines[1]
    assert click.lines[2].startswith("  - ")
    assert "winter-service-docker" in click.lines[2]


def test_stream_multi_provider_order_preserved() -> None:
    """Stream reporter preserves declared order (docker first)."""
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(), _docker_candidate()),
        bound_extension="winter-service-docker",
        bound_extensions=("winter-service-docker", "winter-service-tmux"),
        binding_kind="explicit",
        error=None,
    )
    StreamCapabilityReporter(click).render([resolution])
    # docker should appear before tmux in the indented list
    docker_line_idx = next(i for i, line in enumerate(click.lines) if "docker" in line and line.startswith("  - "))
    tmux_line_idx = next(i for i, line in enumerate(click.lines) if "tmux" in line and line.startswith("  - "))
    assert docker_line_idx < tmux_line_idx


def test_json_multi_provider_bound_is_array() -> None:
    """JSON reporter emits `bound` as an array for multi-provider slots (D4)."""
    click = FakeClick()
    resolution = _multi_resolution(
        bound_extensions=("winter-service-tmux", "winter-service-docker"),
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    assert isinstance(obj["bound"], list)
    assert obj["bound"] == ["winter-service-tmux", "winter-service-docker"]
    assert obj["binding_kind"] == "explicit"


def test_json_single_provider_bound_is_scalar() -> None:
    """JSON reporter keeps `bound` as a scalar string for single-provider slots (D4)."""
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(),),
        bound_extension="winter-service-tmux",
        bound_extensions=("winter-service-tmux",),
        binding_kind="explicit",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    # Single provider → scalar (not array)
    assert isinstance(obj["bound"], str)
    assert obj["bound"] == "winter-service-tmux"


def test_json_no_ordered_list_bound_is_scalar() -> None:
    """JSON reporter keeps scalar `bound` when bound_extensions is empty (legacy path)."""
    click = FakeClick()
    resolution = SlotResolution(
        slot=CapabilitySlot.service,
        candidates=(_tmux_candidate(),),
        bound_extension="winter-service-tmux",
        bound_extensions=(),
        binding_kind="explicit",
        error=None,
    )
    JsonCapabilityReporter(click).render([resolution])
    payload = json.loads(click.lines[0])
    obj = payload[0]
    assert isinstance(obj["bound"], str)
    assert obj["bound"] == "winter-service-tmux"
