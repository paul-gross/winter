from __future__ import annotations

import json
from typing import Any, Protocol

from winter_cli.modules.capability.models import SlotResolution


class ICapabilityReporter(Protocol):
    """Sink for a list of slot resolutions — rendered in a single call."""

    def render(self, resolutions: list[SlotResolution]) -> None: ...


class StreamCapabilityReporter:
    """Renders capability slot resolutions as human-readable lines."""

    def __init__(self, click: Any) -> None:
        self._click = click

    def render(self, resolutions: list[SlotResolution]) -> None:
        for resolution in resolutions:
            slot_name = resolution.slot.value
            kind = resolution.binding_kind

            if kind == "explicit":
                bound = resolution.bound_extension
                candidate = next((c for c in resolution.candidates if c.extension_name == bound), None)
                if candidate is None:
                    self._click.echo(f"{slot_name} → {bound} (explicit)  [candidate not found]")
                else:
                    valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                    self._click.echo(f"{slot_name} → {bound} (explicit)  [{candidate.entrypoint_rel} {valid_glyph}]")

            elif kind == "implicit":
                candidate = resolution.candidates[0]
                valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                self._click.echo(
                    f"{slot_name} → {candidate.extension_name} (implicit)  [{candidate.entrypoint_rel} {valid_glyph}]"
                )

            elif kind == "invalid":
                bound = resolution.bound_extension
                self._click.echo(f"{slot_name} → {bound} (invalid)  — {resolution.error}")

            else:
                # unbound
                if resolution.is_ambiguous:
                    names = ", ".join(c.extension_name for c in resolution.candidates)
                    self._click.echo(f"{slot_name} → (unbound — {len(resolution.candidates)} candidates: {names})")
                    for candidate in resolution.candidates:
                        valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                        self._click.echo(f"  - {candidate.extension_name}  [{candidate.entrypoint_rel} {valid_glyph}]")
                else:
                    self._click.echo(f"{slot_name} → (no provider installed)")


class JsonCapabilityReporter:
    """Emits capability slot resolutions as a single JSON array.

    Stable machine contract — one object per slot:
    {"slot": "service", "bound": "...", "binding_kind": "...", "ambiguous": false,
     "error": null, "candidates": [{"extension": "...", "entrypoint": "...", "valid": true}]}

    Slots emitted in CapabilitySlot declaration order.
    """

    def __init__(self, click: Any) -> None:
        self._click = click

    def render(self, resolutions: list[SlotResolution]) -> None:
        payload = [
            {
                "slot": r.slot.value,
                "bound": r.bound_extension,
                "binding_kind": r.binding_kind,
                "ambiguous": r.is_ambiguous,
                "error": r.error,
                "candidates": [
                    {
                        "extension": c.extension_name,
                        "entrypoint": c.entrypoint_rel,
                        "valid": c.entrypoint_valid,
                    }
                    for c in r.candidates
                ],
            }
            for r in resolutions
        ]
        self._click.echo(json.dumps(payload))
