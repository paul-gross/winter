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
                # Multi-provider list case.
                if len(resolution.bound_extensions) > 1:
                    names = ", ".join(resolution.bound_extensions)
                    self._click.echo(f"{slot_name} → [{names}] (explicit)")
                    for name in resolution.bound_extensions:
                        candidate = next((c for c in resolution.candidates if c.extension_name == name), None)
                        if candidate is None:
                            self._click.echo(f"  - {name}  [candidate not found]")
                        else:
                            valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                            self._click.echo(f"  - {name}  [{candidate.entrypoint_rel} {valid_glyph}]")
                else:
                    bound = resolution.bound_extension
                    candidate = next((c for c in resolution.candidates if c.extension_name == bound), None)
                    if candidate is None:
                        self._click.echo(f"{slot_name} → {bound} (explicit)  [candidate not found]")
                    else:
                        valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                        self._click.echo(
                            f"{slot_name} → {bound} (explicit)  [{candidate.entrypoint_rel} {valid_glyph}]"
                        )

            elif kind == "implicit":
                if len(resolution.bound_extensions) > 1:
                    # Implicit-all: 2+ self-registered candidates, no explicit binding.
                    names = ", ".join(resolution.bound_extensions)
                    self._click.echo(f"{slot_name} → [{names}] (implicit, all candidates)")
                    for name in resolution.bound_extensions:
                        candidate = next((c for c in resolution.candidates if c.extension_name == name), None)
                        if candidate is None:
                            self._click.echo(f"  - {name}  [candidate not found]")
                        else:
                            valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                            self._click.echo(f"  - {name}  [{candidate.entrypoint_rel} {valid_glyph}]")
                else:
                    candidate = resolution.candidates[0]
                    valid_glyph = "✓" if candidate.entrypoint_valid else "✗"
                    detail = f"[{candidate.entrypoint_rel} {valid_glyph}]"
                    self._click.echo(f"{slot_name} → {candidate.extension_name} (implicit)  {detail}")

            elif kind == "invalid":
                bound = resolution.bound_extension
                if len(resolution.bound_extensions) > 1:
                    names = ", ".join(resolution.bound_extensions)
                    self._click.echo(f"{slot_name} → [{names}] (invalid)  — {resolution.error}")
                else:
                    self._click.echo(f"{slot_name} → {bound} (invalid)  — {resolution.error}")

            elif kind == "incompatible":
                bound = resolution.bound_extension
                if len(resolution.bound_extensions) > 1:
                    names = ", ".join(resolution.bound_extensions)
                    self._click.echo(f"{slot_name} → [{names}] (incompatible)  — {resolution.error}")
                else:
                    self._click.echo(f"{slot_name} → {bound} (incompatible)  — {resolution.error}")

            else:
                # unbound — zero providers installed
                self._click.echo(f"{slot_name} → (no provider installed)")


class JsonCapabilityReporter:
    """Emits capability slot resolutions as a single JSON array.

    Stable machine contract — one object per slot:
    {"slot": "service", "bound": "...", "binding_kind": "...", "ambiguous": false,
     "error": null, "candidates": [{"extension": "...", "entrypoint": "...", "valid": true}]}

    D4 contract: `bound` is a scalar string for single-provider slots and a JSON
    array for multi-provider slots (len(bound_extensions) > 1). Single-provider
    slots keep the existing scalar shape so the machine contract is not broken.

    Slots emitted in CapabilitySlot declaration order.
    """

    def __init__(self, click: Any) -> None:
        self._click = click

    @staticmethod
    def _bound_value(r: SlotResolution) -> object:
        """Return the `bound` JSON value: scalar for single, array for multi (D4)."""
        if len(r.bound_extensions) > 1:
            return list(r.bound_extensions)
        return r.bound_extension

    def render(self, resolutions: list[SlotResolution]) -> None:
        payload = [
            {
                "slot": r.slot.value,
                "bound": self._bound_value(r),
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
