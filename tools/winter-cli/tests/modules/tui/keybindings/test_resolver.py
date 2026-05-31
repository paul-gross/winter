"""Keybinding resolver: config overrides, classification, error reporting (issue/23).

Covers single-key override, modifier-chord override, `<leader>` sequences routed
to the chord engine, the single-key-that-is-also-a-sequence-prefix split, plugin
literal defaults, invalid-spec fallback with a located error, and unknown action
id reporting.
"""

from __future__ import annotations

from winter_cli.config.models import KeybindingsConfig
from winter_cli.modules.tui.keybindings.actions import ActionBinding
from winter_cli.modules.tui.keybindings.resolver import KeybindingResolver


def _resolver(bindings=None, leader="\\", timeoutlen=1000) -> KeybindingResolver:
    return KeybindingResolver(KeybindingsConfig(leader=leader, timeoutlen=timeoutlen, bindings=bindings or {}))


def _binding(action_id: str, default: str, **kw) -> ActionBinding:
    return ActionBinding(action_id, default, action_id.split(".")[-1], "desc", **kw)


def test_default_used_when_no_override() -> None:
    res = _resolver().resolve([_binding("workspace.refresh", "r")])
    assert [rb.token for rb in res.natives] == ["r"]
    assert res.errors == []


def test_single_key_override() -> None:
    res = _resolver({"workspace.refresh": "g"}).resolve([_binding("workspace.refresh", "r")])
    assert res.natives[0].token == "g"


def test_modifier_chord_override_normalizes() -> None:
    res = _resolver({"workspace.refresh": "<C-s>"}).resolve([_binding("workspace.refresh", "r")])
    assert res.natives[0].token == "ctrl+s"


def test_leader_override_becomes_sequence() -> None:
    res = _resolver({"workspace.sync": "<leader>S"}).resolve([_binding("workspace.sync", "s")])
    assert res.natives == []
    assert len(res.sequences) == 1
    assert res.sequences[0].spec.keys == ("backslash", "S")


def test_timeoutlen_surfaced() -> None:
    assert _resolver(timeoutlen=250).timeoutlen_ms == 250


def test_prefix_single_key_routed_to_engine() -> None:
    # `g` is a single binding and also the prefix of the `gd` sequence, so it is
    # classified as an engine single (not a native binding) for timeout handling.
    res = _resolver({"a.top": "g", "a.goto": "gd"}).resolve([_binding("a.top", "g"), _binding("a.goto", "x")])
    assert [rb.binding.action_id for rb in res.engine_singles] == ["a.top"]
    assert [rb.binding.action_id for rb in res.sequences] == ["a.goto"]
    assert res.natives == []


def test_plugin_default_is_literal_token() -> None:
    res = _resolver().resolve([_binding("plugin.diff", "enter", default_is_token=True)])
    # "enter" stays one key rather than splitting into e-n-t-e-r.
    assert res.natives[0].spec.keys == ("enter",)


def test_invalid_override_falls_back_to_default_with_error() -> None:
    res = _resolver({"workspace.refresh": "<C->"}).resolve([_binding("workspace.refresh", "r")])
    assert res.natives[0].token == "r"  # fell back to default
    assert len(res.errors) == 1
    assert "workspace.refresh" in res.errors[0]
    assert "<C->" in res.errors[0]


def test_invalid_override_does_not_drop_other_bindings() -> None:
    res = _resolver({"a.one": "<bogus>"}).resolve([_binding("a.one", "r"), _binding("a.two", "L")])
    tokens = {rb.binding.action_id: rb.token for rb in res.natives}
    assert tokens == {"a.one": "r", "a.two": "L"}
    assert len(res.errors) == 1


def test_unknown_action_id_reported() -> None:
    resolver = _resolver({"workspace.refresh": "r", "made.up": "x"})
    errors = resolver.unknown_id_errors({"workspace.refresh", "app.quit"})
    assert len(errors) == 1
    assert "made.up" in errors[0]


def test_same_trigger_collision_reported() -> None:
    res = _resolver({"a.two": "r"}).resolve([_binding("a.one", "r"), _binding("a.two", "x")])
    assert len(res.errors) == 1
    assert "a.one" in res.errors[0] and "a.two" in res.errors[0]


def test_shared_prefix_is_not_a_collision() -> None:
    # `g` and `gd` share a prefix key but are distinct triggers — not flagged.
    res = _resolver({"a.top": "g", "a.goto": "gd"}).resolve([_binding("a.top", "g"), _binding("a.goto", "x")])
    assert res.errors == []
