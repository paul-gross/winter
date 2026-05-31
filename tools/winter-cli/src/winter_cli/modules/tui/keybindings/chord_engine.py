"""Pure matcher for Neovim-style multi-key chord sequences.

The engine is deliberately timer-free so it is fully unit-testable: `feed(key)`
advances the match on each key press, and `timeout()` resolves a pending
sequence when the inter-key deadline lapses. The Textual screen owns the actual
`timeoutlen` timer (see `mixin.KeybindingMixin`) and calls these two methods.

It tracks two kinds of binding:
  - `sequences` — multi-key token tuples (`("backslash", "S")`, `("g", "d")`).
  - `singles`   — single tokens that are *also* a sequence's prefix key and so
                  must be intercepted here rather than bound natively (e.g. `g`
                  is bound to an action and is also the first key of `gd`).

Resolution mirrors Neovim's `timeoutlen` behavior: a key that completes a
sequence fires immediately; a key that is only a prefix starts a pending state;
if the deadline lapses on a pending prefix that is itself a complete binding,
that prefix binding fires (`timeout()`).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ChordOutcome:
    """Result of feeding a key (or a timeout) to the engine.

    `consume` — swallow the key event so no native binding also handles it. When
        False, the screen lets Textual's normal binding dispatch run for this key
        (it is irrelevant to any chord, or it *broke* a pending sequence and is
        itself a native binding that must still fire).
    `pending` — a sequence is now in progress; the screen should (re)arm the
        inter-key timer.
    `fire` — Textual action targets to dispatch now, in order. May be non-empty
        even when `consume` is False: a key that breaks a pending sequence fires
        the held prefix's binding *and* falls through to its own native binding.
    """

    consume: bool
    pending: bool
    fire: tuple[str, ...] = ()


_IGNORE = ChordOutcome(consume=False, pending=False)


class ChordEngine:
    def __init__(
        self,
        sequences: dict[tuple[str, ...], str],
        singles: dict[str, str],
    ) -> None:
        self._sequences = dict(sequences)
        self._singles = dict(singles)
        self._pending: tuple[str, ...] = ()

    @property
    def pending(self) -> tuple[str, ...]:
        return self._pending

    def reset(self) -> None:
        self._pending = ()

    def feed(self, key: str) -> ChordOutcome:
        candidate = (*self._pending, key)
        extensions = [seq for seq in self._sequences if _starts_with(seq, candidate)]

        completes = candidate in self._sequences
        extendable = any(len(seq) > len(candidate) for seq in extensions)

        if completes and not extendable:
            self._pending = ()
            return ChordOutcome(consume=True, pending=False, fire=(self._sequences[candidate],))

        if extensions:
            # Prefix of a longer sequence (and maybe a complete binding itself,
            # resolved on timeout). Wait for the next key.
            self._pending = candidate
            return ChordOutcome(consume=True, pending=True)

        # `candidate` matches nothing.
        if self._pending:
            # A key broke the pending sequence: resolve the prefix that was held,
            # then reprocess this key as a fresh first key. Crucially we inherit
            # the fresh key's `consume`/`pending` — so a breaking key that is a
            # plain native binding (not an engine token) is NOT swallowed: the
            # held prefix fires AND the key falls through to its native action.
            prefix_fire = self._resolve_pending()
            self._pending = ()
            nxt = self.feed(key)
            return ChordOutcome(consume=nxt.consume, pending=nxt.pending, fire=prefix_fire + nxt.fire)

        # Fresh key, no pending state.
        if key in self._singles:
            return ChordOutcome(consume=True, pending=False, fire=(self._singles[key],))
        return _IGNORE

    def timeout(self) -> ChordOutcome:
        """Inter-key deadline lapsed: fire the pending prefix's binding, if any."""
        fire = self._resolve_pending()
        self._pending = ()
        return ChordOutcome(consume=True, pending=False, fire=fire)

    def _resolve_pending(self) -> tuple[str, ...]:
        if self._pending in self._sequences:
            return (self._sequences[self._pending],)
        if len(self._pending) == 1 and self._pending[0] in self._singles:
            return (self._singles[self._pending[0]],)
        return ()


def _starts_with(seq: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(seq) >= len(prefix) and seq[: len(prefix)] == prefix
