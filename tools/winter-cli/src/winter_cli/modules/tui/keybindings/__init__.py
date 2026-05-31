"""Configurable keybindings for the winter dashboard.

This package adds a configuration layer over Textual's binding system so users
can remap built-in and plugin action keys from `.winter/config.toml`, plus a
core chord engine for Neovim-style multi-key sequences (`<leader>S`, `gd`).

Layers:
  - `keyspec`     — parse a key-spec string into normalized Textual key tokens.
  - `actions`     — stable action ids + their hardcoded defaults, per screen.
  - `resolver`    — merge config overrides onto defaults, split single keys from
                    multi-key sequences, and report invalid specs.
  - `chord_engine`— pure, timer-free matcher for multi-key sequences.
  - `mixin`       — `KeybindingMixin`: installs resolved bindings on a screen and
                    drives the chord engine from Textual key events.
"""

from __future__ import annotations

from winter_cli.modules.tui.keybindings.actions import (
    ActionBinding,
    all_builtin_action_ids,
    plugin_action_bindings,
)
from winter_cli.modules.tui.keybindings.chord_engine import ChordEngine, ChordOutcome
from winter_cli.modules.tui.keybindings.keyspec import KeySpec, KeySpecError, parse_key_spec
from winter_cli.modules.tui.keybindings.mixin import KeybindingMixin
from winter_cli.modules.tui.keybindings.resolver import (
    BindingResolution,
    KeybindingResolver,
    ResolvedBinding,
)

__all__ = [
    "ActionBinding",
    "BindingResolution",
    "ChordEngine",
    "ChordOutcome",
    "KeySpec",
    "KeySpecError",
    "KeybindingMixin",
    "KeybindingResolver",
    "ResolvedBinding",
    "all_builtin_action_ids",
    "parse_key_spec",
    "plugin_action_bindings",
]
