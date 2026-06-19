"""Merge config overrides onto action defaults and split keys from sequences.

`KeybindingResolver` is built once from the workspace `KeybindingsConfig` and
asked to `resolve` a screen's `ActionBinding`s. For each action it picks the
configured key spec (or the default), parses it, and classifies the result:

  - `natives`        — single-key bindings whose token does not start any
                       sequence; bound directly through Textual.
  - `engine_singles` — single-key bindings whose token *is* a sequence prefix;
                       routed through the chord engine so the engine can hold the
                       key as a pending prefix and fire it on timeout.
  - `sequences`      — multi-key bindings handled by the chord engine.

An invalid override is reported in `errors` and falls back to the (always valid)
default, so a single bad spec never drops the rest of the bindings.
"""

from __future__ import annotations

import dataclasses

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings.actions import ActionBinding
from winter_cli.modules.tui.keybindings.keyspec import (
    KeySpec,
    KeySpecError,
    parse_key_spec,
    spec_from_textual_token,
)


@dataclasses.dataclass(frozen=True)
class ResolvedBinding:
    binding: ActionBinding
    spec: KeySpec

    @property
    def token(self) -> str:
        """First (and, for single bindings, only) token."""
        return self.spec.keys[0]


@dataclasses.dataclass
class BindingResolution:
    natives: list[ResolvedBinding]
    engine_singles: list[ResolvedBinding]
    sequences: list[ResolvedBinding]
    errors: list[str]


class KeybindingResolver:
    def __init__(self, config: KeybindingsConfig) -> None:
        self._config = config

    @property
    def timeoutlen_ms(self) -> int:
        return self._config.timeoutlen

    def unknown_id_errors(self, known_ids: set[str]) -> list[str]:
        """Report config bindings whose action id matches no known action."""
        return [
            f"keybindings: unknown action id {action_id!r} in config; ignored"
            for action_id in self._config.bindings
            if action_id not in known_ids
        ]

    def resolve(self, actions: list[ActionBinding]) -> BindingResolution:
        resolved: list[ResolvedBinding] = []
        errors: list[str] = []

        for action in actions:
            override = self._config.bindings.get(action.action_id)
            if override is not None:
                # Config overrides always use the user-facing spec grammar.
                try:
                    spec = parse_key_spec(override, leader=self._config.leader)
                except KeySpecError as exc:
                    errors.append(
                        f"keybindings: action {action.action_id!r}: invalid key spec "
                        f"{override!r} ({exc}); using default {action.default!r}"
                    )
                    spec = self._default_spec(action)
            else:
                spec = self._default_spec(action)
            resolved.append(ResolvedBinding(binding=action, spec=spec))

        errors.extend(self._collision_errors(resolved))

        sequence_tokens = {token for rb in resolved if rb.spec.is_sequence for token in rb.spec.keys}

        natives: list[ResolvedBinding] = []
        engine_singles: list[ResolvedBinding] = []
        sequences: list[ResolvedBinding] = []
        for rb in resolved:
            if rb.spec.is_sequence:
                sequences.append(rb)
            elif rb.token in sequence_tokens:
                engine_singles.append(rb)
            else:
                natives.append(rb)

        return BindingResolution(
            natives=natives,
            engine_singles=engine_singles,
            sequences=sequences,
            errors=errors,
        )

    @staticmethod
    def _collision_errors(resolved: list[ResolvedBinding]) -> list[str]:
        """Report distinct actions that resolve to the same exact trigger.

        Two bindings with identical key tuples both reach `self._bindings.bind`
        (or the chord engine) and the second silently wins. A shared *prefix*
        (`g` and `gd`) is intentional and not a collision — only identical full
        key tuples are flagged.

        Scope-aware: two bindings conflict iff their scope sets intersect, where
        an empty scope set (built-in actions) is a wildcard that intersects
        everything. Disjoint plugin scope sets on the same key are NOT a
        collision.
        """
        by_keys: dict[tuple[str, ...], list[ResolvedBinding]] = {}
        for rb in resolved:
            by_keys.setdefault(rb.spec.keys, []).append(rb)
        errors: list[str] = []
        for keys, rbs in by_keys.items():
            if len(rbs) < 2:
                continue
            spec = "+".join(keys) if len(keys) == 1 else " ".join(keys)
            # Check every pair for scope intersection.
            # Collect conflicting pairs grouped by their intersecting areas so
            # we emit one deterministic error per area cluster rather than O(N^2) noise.
            # area_cluster -> set of action_ids involved in that cluster.
            # Empty scopes = wildcard; those always produce a built-in-style error.
            wildcard_ids: set[str] = set()
            # For plugin-vs-plugin overlaps, group by frozenset of intersecting area values.
            area_to_ids: dict[frozenset[str], set[str]] = {}
            for i in range(len(rbs)):
                for j in range(i + 1, len(rbs)):
                    a, b = rbs[i].binding, rbs[j].binding
                    if not a.scopes or not b.scopes:
                        # At least one side is a wildcard (built-in) — always conflicts.
                        wildcard_ids.add(a.action_id)
                        wildcard_ids.add(b.action_id)
                    else:
                        overlap = a.scopes & b.scopes
                        if overlap:
                            area_key = frozenset(s.value for s in overlap)
                            area_to_ids.setdefault(area_key, set()).add(a.action_id)
                            area_to_ids.setdefault(area_key, set()).add(b.action_id)
            if wildcard_ids:
                errors.append(
                    f"keybindings: {', '.join(sorted(wildcard_ids))} all bind {spec!r}; only the last takes effect"
                )
            for area_key, ids in sorted(area_to_ids.items(), key=lambda kv: sorted(kv[0])):
                # Exclude ids already reported in the wildcard error to avoid double-reporting.
                remaining = ids - wildcard_ids
                if len(remaining) < 2:
                    continue
                areas = ", ".join(sorted(area_key))
                ids_str = ", ".join(sorted(remaining))
                errors.append(f"keybindings: {ids_str} all bind {spec!r} in {areas}; only the last takes effect")
        return errors

    def _default_spec(self, action: ActionBinding) -> KeySpec:
        """Parse an action's default. Built-in defaults use the spec grammar;
        plugin defaults are literal Textual tokens (taken verbatim)."""
        if action.default_is_token:
            return spec_from_textual_token(action.default)
        # Built-in defaults are authored in-tree and are always valid.
        return parse_key_spec(action.default, leader=self._config.leader)
