"""Key-spec parsing and normalization (issue/23).

Covers single keys, the `<C-_>`/`<A-_>`/`<M-_>`/`<S-_>` modifier chords and their
combinations, `<leader>` expansion, multi-key sequences, and the located errors
raised for malformed specs.
"""

from __future__ import annotations

import pytest

from winter_cli.modules.tui.keybindings.keyspec import (
    KeySpecError,
    parse_key_spec,
    spec_from_textual_token,
)

LEADER = "\\"


def _keys(spec: str, leader: str = LEADER) -> tuple[str, ...]:
    return parse_key_spec(spec, leader=leader).keys


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("s", ("s",)),
        ("D", ("D",)),
        ("<C-s>", ("ctrl+s",)),
        ("<A-s>", ("alt+s",)),
        ("<M-s>", ("alt+s",)),  # meta normalizes to alt
        ("<S-s>", ("S",)),  # shift on a letter is the uppercase letter
        ("<C-A-s>", ("ctrl+alt+s",)),
        ("<C-S-1>", ("ctrl+shift+1",)),  # shift on a non-letter keeps shift+
        ("<CR>", ("enter",)),
        ("<C-Tab>", ("ctrl+tab",)),
        ("<space>", ("space",)),
    ],
)
def test_single_and_modifier_chords(spec: str, expected: tuple[str, ...]) -> None:
    assert _keys(spec) == expected


def test_modifier_case_insensitive_and_lowercased_base() -> None:
    # <C-S> means ctrl+s (base letter lowercased), not ctrl+shift.
    assert _keys("<C-S>") == ("ctrl+s",)
    assert _keys("<c-s>") == ("ctrl+s",)


def test_backslash_normalizes_to_textual_token() -> None:
    # The raw `\` char must match Textual's runtime key name `backslash`.
    assert _keys("\\") == ("backslash",)


def test_leader_expansion_default() -> None:
    assert _keys("<leader>S") == ("backslash", "S")
    assert _keys("<leader>gd") == ("backslash", "g", "d")


def test_leader_expansion_custom_token() -> None:
    assert parse_key_spec("<leader>x", leader="<C-a>").keys == ("ctrl+a", "x")
    assert parse_key_spec("<leader>w", leader=",").keys == ("comma", "w")


def test_bare_sequence() -> None:
    assert _keys("gd") == ("g", "d")


def test_function_keys() -> None:
    assert _keys("<f1>") == ("f1",)
    assert _keys("<F12>") == ("f12",)


def test_leader_may_not_reference_leader() -> None:
    with pytest.raises(KeySpecError):
        parse_key_spec("<leader>", leader="<leader>")


@pytest.mark.parametrize(
    "spec",
    ["", "   ", "<C->", "<bogus>", "a<", "<>", "<C-X-s>"],
)
def test_invalid_specs_raise(spec: str) -> None:
    with pytest.raises(KeySpecError):
        _keys(spec)


def test_error_names_the_offending_spec() -> None:
    with pytest.raises(KeySpecError, match="bogus"):
        _keys("<bogus>")


def test_spec_from_textual_token_is_literal() -> None:
    # Plugin keys are raw Textual tokens — "enter" must stay one key, not split.
    assert spec_from_textual_token("enter").keys == ("enter",)
    assert spec_from_textual_token("ctrl+e").keys == ("ctrl+e",)
    assert spec_from_textual_token("e").keys == ("e",)
    assert spec_from_textual_token("\\").keys == ("backslash",)
