"""`KeybindingMixin` — installs resolved bindings and runs the chord engine.

A dashboard screen mixes this in alongside `PluginActionMixin` and calls
`_install_keybindings(actions)` from `on_mount`. The mixin binds single-key
specs natively through Textual and feeds multi-key sequences (and their prefix
keys) to a `ChordEngine`, driving the inter-key `timeoutlen` timer from Textual
key events.

Host requirements (a Textual `Screen` subclass provides all of these):
  - `self._keybinding_resolver` — the injected `KeybindingResolver`
  - `self._bindings`, `self.set_timer`, `self.run_action` — from Textual
"""

from __future__ import annotations

from textual import events

from winter_cli.modules.tui.keybindings.actions import ActionBinding
from winter_cli.modules.tui.keybindings.chord_engine import ChordEngine
from winter_cli.modules.tui.keybindings.resolver import KeybindingResolver


class KeybindingMixin:
    _keybinding_resolver: KeybindingResolver

    def _install_keybindings(self, actions: list[ActionBinding]) -> list[str]:
        """Resolve and install `actions`; return any spec errors for surfacing."""
        resolution = self._keybinding_resolver.resolve(actions)

        for rb in resolution.natives:
            self._bindings.bind(  # type: ignore[attr-defined]
                rb.token,
                rb.binding.action,
                rb.binding.description,
                show=rb.binding.show,
            )

        sequences = {rb.spec.keys: rb.binding.action for rb in resolution.sequences}
        singles = {rb.token: rb.binding.action for rb in resolution.engine_singles}
        self._chord: ChordEngine | None = ChordEngine(sequences, singles) if (sequences or singles) else None
        self._chord_timeout_s: float = self._keybinding_resolver.timeoutlen_ms / 1000
        self._chord_timer = None

        return resolution.errors

    async def on_key(self, event: events.Key) -> None:
        chord = getattr(self, "_chord", None)
        if chord is None:
            return

        outcome = chord.feed(event.key)
        if not outcome.consume and not outcome.fire:
            # Irrelevant key — let Textual's native binding dispatch handle it.
            return

        if outcome.consume:
            event.stop()
            event.prevent_default()
        self._cancel_chord_timer()
        if outcome.pending:
            self._chord_timer = self.set_timer(self._chord_timeout_s, self._on_chord_timeout)  # type: ignore[attr-defined]
        for target in outcome.fire:
            # A broken-sequence prefix fires here while `consume` is False, so the
            # breaking key still falls through to its own native binding.
            await self.run_action(target)  # type: ignore[attr-defined]

    async def _on_chord_timeout(self) -> None:
        self._chord_timer = None
        chord = getattr(self, "_chord", None)
        if chord is None:
            return
        for target in chord.timeout().fire:
            await self.run_action(target)  # type: ignore[attr-defined]

    def _cancel_chord_timer(self) -> None:
        timer = getattr(self, "_chord_timer", None)
        if timer is not None:
            timer.stop()
            self._chord_timer = None
